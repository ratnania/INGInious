# -*- coding: utf-8 -*-
#
# This file is part of INGInious. See the LICENSE and the COPYRIGHTS files for
# more information about the licensing of this file.

""" Course page """
import web
import logging

from collections import OrderedDict
from inginious.frontend.courses import WebAppCourse
from inginious.frontend.tasks import WebAppTask
from inginious.frontend.pages.utils import INGIniousAuthPage


def handle_course_unavailable(app_homepath, template_helper, user_manager, course):
    """ Displays the course_unavailable page or the course registration page """
    reason = user_manager.course_is_open_to_user(course, lti=False, return_reason=True)
    if reason == "unregistered_not_previewable":
        username = user_manager.session_username()
        user_info = user_manager.get_user_info(username)
        if course.is_registration_possible(user_info):
            raise web.seeother(app_homepath + "/register/" + course.get_id())
    return template_helper.get_renderer(use_jinja=True).course_unavailable(reason=reason)


class CoursePage(INGIniousAuthPage):
    """ Course page """
    _logger = logging.getLogger("inginious.webapp.course")

    def preview_allowed(self, courseid):
        course = self.get_course(courseid)
        return course.get_accessibility().is_open() and course.allow_preview()

    def get_course(self, courseid):
        """ Return the course """
        try:
            course = self.database.courses.find_one({"_id": courseid})
            course = WebAppCourse(course["_id"], course, self.filesystem, self.plugin_manager)
        except:
            raise web.notfound()

        return course

    def POST_AUTH(self, courseid):  # pylint: disable=arguments-differ
        """ POST request """
        course = self.get_course(courseid)

        user_input = web.input()
        if "unregister" in user_input and course.allow_unregister():
            self.user_manager.course_unregister_user(course, self.user_manager.session_username())
            raise web.seeother(self.app.get_homepath() + '/mycourses')

        return self.show_page(course)

    def GET_AUTH(self, courseid):  # pylint: disable=arguments-differ
        """ GET request """
        course = self.get_course(courseid)
        return self.show_page(course)

    def show_page(self, course):
        """ Prepares and shows the course page """
        username = self.user_manager.session_username()
        if not self.user_manager.course_is_open_to_user(course, lti=False):
            return handle_course_unavailable(self.app.get_homepath(), self.template_helper, self.user_manager, course)
        else:
            task_descs = self.database.tasks.find({"courseid": course.get_id()}).sort("order")
            tasks = OrderedDict()
            for task_desc in task_descs:
                try:
                    tasks[task_desc["taskid"]] = WebAppTask(course.get_id(), task_desc["taskid"],
                                                            task_desc, self.filesystem, self.plugin_manager,
                                                            self.problem_types)
                except Exception as e:
                    self._logger.warning(e)
            last_submissions = self.submission_manager.get_user_last_submissions(5, {"courseid": course.get_id(), "taskid": {"$in": list(tasks.keys())}})

            for submission in last_submissions:
                    submission["taskname"] = tasks[submission['taskid']].get_name(self.user_manager.session_language())

            tasks_data = {}
            user_tasks = self.database.user_tasks.find({"username": username, "courseid": course.get_id(), "taskid": {"$in": list(tasks.keys())}})
            is_admin = self.user_manager.has_staff_rights_on_course(course, username)

            tasks_score = [0.0, 0.0]

            for taskid, task in tasks.items():
                tasks_data[taskid] = {"visible": task.get_accessible_time(course).after_start() or is_admin, "succeeded": False,
                                      "grade": 0.0}
                tasks_score[1] += task.get_grading_weight() if tasks_data[taskid]["visible"] else 0

            for user_task in user_tasks:
                tasks_data[user_task["taskid"]]["succeeded"] = user_task["succeeded"]
                tasks_data[user_task["taskid"]]["grade"] = user_task["grade"]

                weighted_score = user_task["grade"]*tasks[user_task["taskid"]].get_grading_weight()
                tasks_score[0] += weighted_score if tasks_data[user_task["taskid"]]["visible"] else 0

            course_grade = round(tasks_score[0]/tasks_score[1]) if tasks_score[1] > 0 else 0
            tag_list = course.get_tags()
            user_info = self.database.users.find_one({"username": username})

            return self.template_helper.get_renderer().course(user_info, course, last_submissions, tasks, tasks_data, course_grade, tag_list)
