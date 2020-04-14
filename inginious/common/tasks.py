# -*- coding: utf-8 -*-
#
# This file is part of INGInious. See the LICENSE and the COPYRIGHTS files for
# more information about the licensing of this file.

""" Task """
import gettext

from inginious.common.base import id_checker
from inginious.common.hook_manager import HookManager


def _migrate_from_v_0_6(content):
    """ Migrate a v0.6 task description to a v0.7+ task description, if needed """
    if "environment" in content:
        content["environment_id"] = content["environment"]
        content["environment_type"] = "docker" if content["environment_id"] != "mcq" else "mcq"
        del content["environment"]
        content["environment_parameters"] = {"limits": content.get("limits", {}),
                                             "run_cmd": content.get("run_cmd", ''),
                                             "network_grading": content.get("network_grading", False),
                                             "response_is_html": content.get('responseIsHTML', False)}
    return content


class Task(object):
    """ Contains the data for a task """

    def __init__(self, courseid, taskid, content, filesystem, hook_manager, task_problem_types):
        """
            Init the task. course is a Course object, taskid the task id, and content is a dictionnary containing the data needed to initialize the Task object.
            If init_data is None, the data will be taken from the course tasks' directory.
        """
        content = _migrate_from_v_0_6(content)

        self._courseid = courseid
        self._taskid = taskid
        self._fs = filesystem
        self._hook_manager = hook_manager
        self._data = content
        self._environment_id = self._data.get('environment_id', 'default')
        self._environment_type = self._data.get('environment_type', 'unknown')
        self._environment_parameters = self._data.get("environment_parameters", {})
        if "problems" not in self._data:
            raise Exception("Tasks must have some problems descriptions")

        # i18n
        self._translations = {}
        self._course_fs = self._fs.from_subfolder(courseid)
        self._course_fs.ensure_exists()
        self._task_fs = self._course_fs.from_subfolder(taskid)
        self._task_fs.ensure_exists()

        self._translations_fs = self._task_fs.from_subfolder("$i18n")

        if not self._translations_fs.exists():
            self._translations_fs = self._task_fs.from_subfolder("student").from_subfolder("$i18n")
        if not self._translations_fs.exists():
            self._translations_fs = self._course_fs.from_subfolder("$common").from_subfolder("$i18n")
        if not self._translations_fs.exists():
            self._translations_fs = self._course_fs.from_subfolder("$common").from_subfolder("student").from_subfolder("$i18n")

        if self._translations_fs.exists():
            for f in self._translations_fs.list(folders=False, files=True, recursive=False):
                lang = f[0:len(f) - 3]
                if self._translations_fs.exists(lang + ".mo"):
                    self._translations[lang] = gettext.GNUTranslations(self._translations_fs.get_fd(lang + ".mo"))
                else:
                    self._translations[lang] = gettext.NullTranslations()

        # Check all problems
        self._problems = []
        for problemid in self._data['problems']:
            self._problems.append(self._create_task_problem(problemid, self._data['problems'][problemid], task_problem_types))

        # Order
        self._order = int(self._data.get('order', -1))

    def get_translation_obj(self, language):
        return self._translations.get(language, gettext.NullTranslations())

    def get_descriptor(self):
        """ Returns the task descriptor """
        return self._data

    def gettext(self, language, *args, **kwargs):
        return self.get_translation_obj(language).gettext(*args, **kwargs)

    def input_is_consistent(self, task_input, default_allowed_extension, default_max_size):
        """ Check if an input for a task is consistent. Return true if this is case, false else """
        for problem in self._problems:
            if not problem.input_is_consistent(task_input, default_allowed_extension, default_max_size):
                return False
        return True

    def get_order(self):
        """ Get the position of this task in the course """
        return self._order

    def get_environment_id(self):
        """ Returns the environment in which the agent have to launch this task"""
        return self._environment_id

    def get_environment_type(self):
        """ Returns the environment type in which the agent have to launch this task"""
        return self._environment_type

    def get_id(self):
        """ Get the id of this task """
        return self._taskid

    def get_problems(self):
        """ Get problems contained in this task """
        return self._problems

    def get_courseid(self):
        """ Return the courseid of the course that contains this task """
        return self._courseid

    def get_environment_parameters(self):
        """ Returns the raw environment parameters, which is a dictionnary that is envtype dependent. """
        return self._environment_parameters

    def get_response_type(self):
        """ Returns the method used to parse the output of the task: HTML or rst """
        return "HTML" if self._environment_parameters.get('response_is_html', False) else "rst"

    def get_fs(self):
        """ Returns a FileSystemProvider which points to the folder of this task """
        return self._task_fs

    def get_hook(self):
        """ Returns the hook manager parameter for this task"""
        return self._hook_manager

    def get_translation_fs(self):
        """ Return the translation_fs parameter for this task"""
        return self._translations_fs

    def check_answer(self, task_input, language):
        """
            Verify the answers in task_input. Returns six values
            1st: True the input is **currently** valid. (may become invalid after running the code), False else
            2nd: True if the input needs to be run in the VM, False else
            3rd: Main message, as a list (that can be join with \n or <br/> for example)
            4th: Problem specific message, as a dictionnary (tuple of result/text)
            5th: Number of subproblems that (already) contain errors. <= Number of subproblems
            6th: Number of errors in MCQ problems. Not linked to the number of subproblems
        """
        valid = True
        need_launch = False
        main_message = []
        problem_messages = {}
        error_count = 0
        multiple_choice_error_count = 0
        for problem in self._problems:
            problem_is_valid, problem_main_message, problem_s_messages, problem_mc_error_count = problem.check_answer(task_input, language)
            if problem_is_valid is None:
                need_launch = True
            elif problem_is_valid == False:
                error_count += 1
                valid = False
            if problem_main_message is not None:
                main_message.append(problem_main_message)
            if problem_s_messages is not None:
                problem_messages[problem.get_id()] = (("success" if problem_is_valid else "failed"), problem_s_messages)
            multiple_choice_error_count += problem_mc_error_count
        return valid, need_launch, main_message, problem_messages, error_count, multiple_choice_error_count

    def _create_task_problem(self, problemid, problem_content, task_problem_types):
        """Creates a new instance of the right class for a given problem."""
        # Basic checks
        if not id_checker(problemid):
            raise Exception("Invalid problem _id: " + problemid)
        if problem_content.get('type', "") not in task_problem_types:
            raise Exception("Invalid type for problem " + problemid)

        return task_problem_types.get(problem_content.get('type', ""))(self, problemid, problem_content)

