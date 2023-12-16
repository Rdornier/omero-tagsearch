from __future__ import absolute_import
from builtins import str
import json
import logging
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.template.loader import render_to_string
from omeroweb.webclient.decorators import render_response, login_required
from omero.sys import Parameters
from omero.rtypes import rlong, rlist
from omeroweb.webclient.views import switch_active_group
from omeroweb.webclient.forms import GlobalSearchForm, ContainerForm
from .forms import TagSearchForm

logger = logging.getLogger(__name__)


@login_required()
@render_response()
def index(request, conn=None, **kwargs):
    request.session.modified = True

    # TODO Hardcode menu as search until I figure out what to do with menu
    menu = "search"
    template = "omero_tagsearch/tagnav.html"

    # tree support
    init = {"initially_open": None, "initially_select": []}
    first_sel = None
    initially_open_owner = None

    # E.g. backwards compatible support for
    # path=project=51|dataset=502|image=607 (select the image)
    path = request.GET.get("path", "")
    i = path.split("|")[-1]
    if i.split("=")[0] in ("project", "dataset", "image", "screen", "plate", "tag", "acquisition", "run", "well"):
        init["initially_select"].append(str(i).replace("=", "-"))

    # Now we support show=image-607|image-123  (multi-objects selected)
    show = request.GET.get("show", "")
    for i in show.split("|"):
        if i.split("-")[0] in (
            "project",
            "dataset",
            "image",
            "screen",
            "plate",
            "tag",
            "acquisition",
            "run",
            "well",
        ):
            # alternatives for 'acquisition'
            i = i.replace("run", "acquisition")
            init["initially_select"].append(str(i))

    if len(init["initially_select"]) > 0:
        # tree hierarchy open to first selected object
        init["initially_open"] = [init["initially_select"][0]]
        first_obj, first_id = init["initially_open"][0].split("-", 1)

        # if we're showing a tag, make sure we're on the tags page...
        if first_obj == "tag" and menu != "usertags":
            return HttpResponseRedirect(
                reverse(viewname="load_template", args=["usertags"])
                + "?show="
                + init["initially_select"][0]
            )

        try:
            # set context to 'cross-group'
            conn.SERVICE_OPTS.setOmeroGroup("-1")
            if first_obj == "tag":
                first_sel = conn.getObject("TagAnnotation", int(first_id))
            else:
                first_sel = conn.getObject(first_obj, int(first_id))
                initially_open_owner = first_sel.details.owner.id.val
                # Wells aren't in the tree, so we need parent...
                if first_obj == "well":
                    parentNode = first_sel.getWellSample().getPlateAcquisition()
                    ptype = "acquisition"
                    # No Acquisition for this well, use Plate instead
                    if parentNode is None:
                        parentNode = first_sel.getParent()
                        ptype = "plate"
                    first_sel = parentNode
                    init["initially_open"] = ["%s-%s" % (ptype, parentNode.getId())]
                    init["initially_select"] = init["initially_open"][:]
        except:
            # invalid id
            pass
        if first_obj not in ("project", "screen"):
            # need to see if first item has parents
            if first_sel is not None:
                for p in first_sel.getAncestry():
                    # parents of tags must be tags (no OMERO_CLASS)
                    if first_obj == "tag":
                        init["initially_open"].insert(0, "tag-%s" % p.getId())
                    else:
                        init["initially_open"].insert(
                            0, "%s-%s" % (p.OMERO_CLASS.lower(), p.getId())
                        )
                        initially_open_owner = p.details.owner.id.val
                if init["initially_open"][0].split("-")[0] == "image":
                    init["initially_open"].insert(0, "orphaned-0")

    # need to be sure that tree will be correct omero.group
    if first_sel is not None:
        switch_active_group(request, first_sel.details.group.id.val)

    # search support
    global_search_form = GlobalSearchForm(data=request.GET.copy())
    if menu == "search":
        if global_search_form.is_valid():
            init["query"] = global_search_form.cleaned_data["search_query"]

    # get url without request string - used to refresh page after switch
    # user/group etc
    url = reverse(viewname="tagsearch")

    # validate experimenter is in the active group
    active_group = request.session.get("active_group") or conn.getEventContext().groupId
    # prepare members of group...
    s = conn.groupSummary(active_group)
    leaders = s["leaders"]
    members = s["colleagues"]
    userIds = [u.id for u in leaders]
    userIds.extend([u.id for u in members])
    users = []
    if len(leaders) > 0:
        users.append(("Owners", leaders))
    if len(members) > 0:
        users.append(("Members", members))
    users = tuple(users)

    # check any change in experimenter...
    user_id = request.GET.get("experimenter")
    if initially_open_owner is not None:
        # if we're not already showing 'All Members'...
        if request.session.get("user_id", None) != -1:
            user_id = initially_open_owner
    try:
        user_id = int(user_id)
    except:
        user_id = None

    # Check is user_id is in a current group
    if (
        user_id not in (set([x.id for x in leaders]) | set([x.id for x in members]))
        and user_id != -1
    ):
        # All users in group is allowed
        user_id = None

    if user_id is None:
        # ... or check that current user is valid in active group
        user_id = request.session.get("user_id", None)
        if user_id is None or int(user_id) not in userIds:
            if user_id != -1:  # All users in group is allowed
                user_id = conn.getEventContext().userId

    request.session["user_id"] = user_id

    if conn.isAdmin():  # Admin can see all groups
        myGroups = [
            g
            for g in conn.getObjects("ExperimenterGroup")
            if g.getName() not in ("user", "guest")
        ]
    else:
        myGroups = list(conn.getGroupsMemberOf())
    myGroups.sort(key=lambda x: x.getName().lower())
    new_container_form = ContainerForm()

    # Create and set the form

    params = Parameters()
    qs = conn.getQueryService()
    service_opts = conn.SERVICE_OPTS.copy()
    service_opts.setOmeroGroup(active_group)

    def get_tags(obj):

        # Get tags
        # It is not sufficient to simply get the objects as there may be tags
        # which are not applied which don't really make sense to display
        # tags = list(self.conn.getObjects("TagAnnotation"))
        hql = (
            """
            SELECT DISTINCT link.child.id, link.child.textValue
            FROM %sAnnotationLink link
            WHERE link.child.class IS TagAnnotation
            ORDER BY link.child.textValue
        """
            % obj
        )

        return [
            (result[0].val, result[1].val)
            for result in qs.projection(hql, params, service_opts)
        ]

    # List of tuples (id, value)
    tags = set(get_tags("Image"))
    tags.update(get_tags("Dataset"))
    tags.update(get_tags("Project"))
    tags.update(get_tags("Plate"))
    tags.update(get_tags("PlateAcquisition"))
    tags.update(get_tags("Screen"))
    tags.update(get_tags("Well"))

    # Convert back to an ordered list and sort
    tags = list(tags)
    tags.sort(key=lambda x: x[1].lower())

    form = TagSearchForm(tags, conn, use_required_attribute=False)

    context = {
        "init": init,
        "myGroups": myGroups,
        "new_container_form": new_container_form,
        "global_search_form": global_search_form,
    }
    context["groups"] = myGroups
    context["active_group"] = conn.getObject("ExperimenterGroup", int(active_group))
    for g in context["groups"]:
        g.groupSummary()  # load leaders / members
    context["active_user"] = conn.getObject("Experimenter", int(user_id))

    context["isLeader"] = conn.isLeader()
    context["current_url"] = url
    context["template"] = template
    context["tagnav_form"] = form

    return context


@login_required(setGroupContext=True)
# TODO Figure out what happened to render_response as it wasn't working on
# production
# @render_response()
def tag_image_search(request, conn=None, **kwargs):
    import time

    start = time.time()
    if request.method == "POST":

        selected_tags = [int(x) for x in request.POST.getlist("selectedTags")]
        excluded_tags = [int(x) for x in request.POST.getlist("excludedTags")]
        operation = request.POST.get("operation")

        # validate experimenter is in the active group
        active_group = (
            request.session.get("active_group") or conn.getEventContext().groupId
        )
        service_opts = conn.SERVICE_OPTS.copy()
        service_opts.setOmeroGroup(active_group)

        def getObjectsWithAnnotations(obj_type, include_ids, exclude_ids):
            # Get the images that match
            params = Parameters()
            params.map = {}
            params.map["incl_ids"] = rlist([rlong(o) for o in set(include_ids)])

            hql = ("select link.parent.id from %sAnnotationLink link "
                   "where link.child.id in (:incl_ids) " % (obj_type))
            if len(exclude_ids) > 0:
                hql += (" and link.parent.id not in "
                        "(select link.parent.id from %sAnnotationLink link "
                        "where link.child.id in (:excl_ids)) " % (obj_type))
                params.map["excl_ids"] = rlist([rlong(o) for o in set(exclude_ids)])

            hql += "group by link.parent.id"
            if operation == "AND":
                hql += f" having count (distinct link.child) = {len(include_ids)}"

            qs = conn.getQueryService()
            return [x[0].getValue() for x in qs.projection(hql, params, service_opts)]

        context = {}
        html_response = ""
        remaining = set([])

        manager = {"containers": {}}
        preview = False
        project_count = 0
        dataset_count = 0
        screen_count = 0
        plate_count = 0
        acquisition_count = 0
        well_count = 0
        image_count = 0
        count_d = {}
        if selected_tags:
            image_ids = getObjectsWithAnnotations("Image", selected_tags, excluded_tags)
            count_d["image"] = len(image_ids)

            dataset_ids = getObjectsWithAnnotations("Dataset", selected_tags, excluded_tags)
            count_d["dataset"] = len(dataset_ids)

            project_ids = getObjectsWithAnnotations("Project", selected_tags, excluded_tags)
            count_d["project"] = len(project_ids)

            screen_ids = getObjectsWithAnnotations("Screen", selected_tags, excluded_tags)
            count_d["screen"] = len(screen_ids)

            plate_ids = getObjectsWithAnnotations("Plate", selected_tags, excluded_tags)
            count_d["plate"] = len(plate_ids)

            well_ids = getObjectsWithAnnotations("Well", selected_tags, excluded_tags)
            count_d["well"] = len(well_ids)

            acquisition_ids = getObjectsWithAnnotations(
                "PlateAcquisition", selected_tags, excluded_tags
            )
            count_d["acquisition"] = len(acquisition_ids)

            if image_ids:
                images = conn.getObjects("Image", ids=image_ids)
                manager["containers"]["image"] = list(images)

            if dataset_ids:
                datasets = conn.getObjects("Dataset", ids=dataset_ids)
                manager["containers"]["dataset"] = list(datasets)

            if project_ids:
                projects = conn.getObjects("Project", ids=project_ids)
                manager["containers"]["project"] = list(projects)

            if screen_ids:
                screens = conn.getObjects("Screen", ids=screen_ids)
                manager["containers"]["screen"] = list(screens)

            if plate_ids:
                plates = conn.getObjects("Plate", ids=plate_ids)
                manager["containers"]["plate"] = list(plates)

            if well_ids:
                wells = []
                for well in  conn.getObjects("Well", ids=well_ids):
                    well.name = well.getParent().name + f" - {well.getWellPos()}"
                    wells.append(well)
                manager["containers"]["well"] = wells

            if acquisition_ids:
                acquisitions = conn.getObjects(
                    "PlateAcquisition", ids=acquisition_ids
                )
                manager["containers"]["acquisition"] = list(acquisitions)

            manager["c_size"] = sum(count_d.values())
            if manager["c_size"] > 0:
                preview = True

            context["manager"] = manager

            html_response = render_to_string(
                "omero_tagsearch/search_details.html", context
            )

            middle = time.time()

            def getAnnotationsForObjects(obj_type, oids):
                # Get the images that match
                params = Parameters()
                params.map = {}
                hql = (
                    "select distinct link.child.id from %sAnnotationLink link " % obj_type
                )
                if operation == "AND":
                    hql += "where link.parent.id in (:oids)"
                    params.map["oids"] = rlist([rlong(o) for o in oids])

                qs = conn.getQueryService()
                return [
                    result[0].val for result in qs.projection(hql, params, service_opts)
                ]

            # Calculate remaining possible tag navigations
            # TODO Compare subquery to pass-in performance
            # sub_hql = """
            #     SELECT link.parent.id
            #     FROM ImageAnnotationLink link
            #     WHERE link.child.id IN (:oids)
            #     GROUP BY link.parent.id
            #     HAVING count (link.parent) = %s
            # """ % len(selected_tags)
            # hql = """
            #     SELECT DISTINCT link.child.id
            #     FROM ImageAnnotationLink link
            #     WHERE link.parent.id IN (%s)
            # """ % sub_hql

            if operation == "AND":
                if image_ids:
                    remaining.update(getAnnotationsForObjects("Image", image_ids))
                if dataset_ids:
                    remaining.update(getAnnotationsForObjects("Dataset", dataset_ids))
                if project_ids:
                    remaining.update(getAnnotationsForObjects("Project", project_ids))
                if well_ids:
                    remaining.update(getAnnotationsForObjects("Well", well_ids))
                if acquisition_ids:
                    remaining.update(
                        getAnnotationsForObjects("PlateAcquisition", acquisition_ids)
                    )
                if plate_ids:
                    remaining.update(getAnnotationsForObjects("Plate", plate_ids))
                if screen_ids:
                    remaining.update(getAnnotationsForObjects("Screen", screen_ids))

            end = time.time()
            logger.info(
                "Tag Query Times. Preview: %ss, Remaining: %ss, Total:%ss"
                % ((middle - start), (end - middle), (end - start))
            )

        # Return the navigation data and the html preview for display
        # return {"navdata": list(remaining), "html": html_response}
        return HttpResponse(
            json.dumps(
                {
                    "navdata": list(remaining),
                    "preview": preview,
                    "count": count_d,
                    "html": html_response,
                }
            ),
            content_type="application/json",
        )
