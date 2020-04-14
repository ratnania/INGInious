# -*- coding: utf-8 -*-
#
# This file is part of INGInious. See the LICENSE and the COPYRIGHTS files for
# more information about the licensing of this file.

import logging

import web
from bson.objectid import ObjectId

from inginious.frontend.pages.course_admin.utils import INGIniousSubmissionAdminPage


class CourseDownloadSubmissions(INGIniousSubmissionAdminPage):
    """ Batch operation management """

    _logger = logging.getLogger("inginious.webapp.download")

    def valid_formats(self):
        dict = {
            "taskid/username": _("taskid/username"),
            "taskid/audience": _("taskid/audience"),
            "username/taskid": _("username/taskid"),
            "audience/taskid": _("audience/taskid")
        }
        return list(dict.keys())

    def POST_AUTH(self, courseid):  # pylint: disable=arguments-differ
        """ GET request """
        course, __ = self.get_course_and_check_rights(courseid)

        user_input = web.input(tasks=[], audiences=[], users=[])

        if "filter_type" not in user_input or "type" not in user_input or "format" not in user_input or user_input.format not in self.valid_formats():
            raise web.notfound()

        task_descs = self.database.tasks.find({"courseid": course.get_id()}).sort("order")
        tasks = [task_desc["taskid"] for task_desc in task_descs]
        for i in user_input.tasks:
            if i not in tasks:
                raise web.notfound()

        # Load submissions
        submissions = self.get_selected_submissions(course,
                                                    only_tasks=user_input.tasks or None,
                                                    only_users=user_input.users if user_input.filter_type == "users" else None,
                                                    only_audiences=user_input.audiences if user_input.filter_type != "users" else None,
                                                    keep_only_evaluation_submissions=user_input.type == "single")

        self._logger.info("Downloading %d submissions from course %s", len(submissions), courseid)
        archive, error = self.submission_manager.get_submission_archive(course, submissions, list(user_input.format.split('/'))+["submissionid"])
        if not error:
            web.header('Content-Type', 'application/x-gzip', unique=True)
            web.header('Content-Disposition', 'attachment; filename="submissions.tgz"', unique=True)
            return archive
        else:
            return self.display_page(course, user_input, _("The following submission could not be prepared for download: {}").format(error))

    def GET_AUTH(self, courseid):  # pylint: disable=arguments-differ
        """ GET request """
        course, __ = self.get_course_and_check_rights(courseid)
        user_input = web.input(tasks=[], aggregations=[], users=[])
        error = ""

        # First, check for a particular submission
        if "submission" in user_input:
            submission = self.database.submissions.find_one({"_id": ObjectId(user_input.submission),
                                                             "courseid": course.get_id(),
                                                             "status": {"$in": ["done", "error"]}})
            if submission is None:
                raise web.notfound()

            self._logger.info("Downloading submission %s - %s - %s - %s", submission['_id'], submission['courseid'],
                              submission['taskid'], submission['username'])
            archive, error = self.submission_manager.get_submission_archive(course, [submission], [])
            if not error:
                web.header('Content-Type', 'application/x-gzip', unique=True)
                web.header('Content-Disposition', 'attachment; filename="submissions.tgz"', unique=True)
                return archive

        # Else, display the complete page
        return self.display_page(course, user_input, error)

    def display_page(self, course, user_input, error):
        tasks, user_data, audiences, tutored_audiences, \
        tutored_users, checked_tasks, checked_users, show_audiences  = self.show_page_params(course, user_input)

        chosen_format = self.valid_formats()[0]
        if "format" in user_input and user_input.format in self.valid_formats():
            chosen_format = user_input.format
            if "audience" in chosen_format:
                show_audiences = True

        return self.template_helper.get_renderer().course_admin.download(course, tasks, user_data, audiences,
                                                                         tutored_audiences, tutored_users,
                                                                         checked_tasks, checked_users,
                                                                         self.valid_formats(), chosen_format,
                                                                         show_audiences, error)
