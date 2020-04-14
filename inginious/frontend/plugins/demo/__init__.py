# -*- coding: utf-8 -*-
#
# This file is part of INGInious. See the LICENSE and the COPYRIGHTS files for
# more information about the licensing of this file.

""" A demo plugin that adds a page """

class DemoPage(object):
    """ A simple demo page showing how to add a new page """

    def GET(self):
        """ GET request """
        return "This is a simple demo plugin"

def init(plugin_manager, _2, _3):
    """ Init the plugin """
    plugin_manager.add_page("/plugindemo", DemoPage)
