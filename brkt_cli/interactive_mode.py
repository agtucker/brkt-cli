# Copyright 2017 Bracket Computing, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# https://github.com/brkt/brkt-cli/blob/master/LICENSE
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and
# limitations under the License.
from abc import abstractmethod, ABCMeta
from getpass import getpass


def format_error(message):
    """
    Formats an error for output on the interactive mode
    :param message: the error message
    :type message: str
    :return: the formatted error
    :rtype: str
    """
    return 'Error: ' + message


class InteractiveArgument(object):
    __metaclass__ = ABCMeta

    def __init__(self):
        """
        The base interactive argument class. All CLI widgets that take user input should go though this
        """
        pass

    @abstractmethod
    def run(self, has_back=False):
        """
        Run the widget and return the value for the argument
        :param has_back: If the argument has the ability to go back (which returns None)
        :type has_back: bool
        :return: the argument value that the user inputted
        :rtype: Any | None
        """
        pass


class InteractiveTextField(InteractiveArgument):
    def __init__(self, prompt):
        """
        Text field argument
        :param prompt: the prompt to ask the user
        :type prompt: str
        """
        self.prompt = prompt
        super(InteractiveTextField, self).__init__()

    def run(self, has_back=False):
        """
        Run the widget and return the value for the argument
        :param has_back: If the argument has the ability to go back (which returns None)
        :type has_back: bool
        :return: the inputted text
        :rtype: str | None
        """
        val = ''
        while val == '':
            print self.prompt + (' (leave blank to go back)' if has_back else '') + ':',
            val = raw_input()
            if val == '' and has_back:
                return None
        return val


class InteractivePasswordField(InteractiveArgument):
    def __init__(self, prompt):
        """
        Password argument
        :param prompt: the prompt to ask the user
        :type prompt: str
        """
        self.prompt = prompt
        super(InteractivePasswordField, self).__init__()

    def run(self, has_back=False):
        """
        Run the widget and return the value for the argument
        :param has_back: If the argument has the ability to go back (which returns None)
        :type has_back: bool
        :return: the inputted password
        :rtype: str | None
        """
        val = ''
        while val == '':
            val = getpass(self.prompt + (' (leave blank to go back)' if has_back else '') + ': ')
            if val == '' and has_back:
                return None
        return val


class InteractiveSelectionNameValueMenu(InteractiveArgument):
    def __init__(self, prompt, options):
        """
        Selection menu argument where you select a name and receive a value to that name
        :param prompt: the prompt to ask the user
        :param options: list of tuples containing the name and value/ID of the option
        :type options: list[(str, (Any | None))]
        """
        self.prompt = prompt
        self.options = options
        super(InteractiveSelectionNameValueMenu, self).__init__()

    def run(self, has_back=False):
        """
        Run the widget and return the value for the argument
        :param has_back: If the argument has the ability to go back (which returns None)
        :type has_back: bool
        :return: the value of the selected option
        :rtype: Any | None
        """
        print self.prompt + ':'
        option_list = self.options

        if has_back:
            option_list.append(('[Back]', None))

        for idx, (name, key) in enumerate(option_list):
            print '%d) %s' % (idx, name)

        val = ''
        val_idx = None
        while val == '':
            print '>',
            val = raw_input()
            if val == '':
                continue

            if not val.isdigit():
                print format_error('Value is not digit')
                val = ''
                continue

            try:
                val_idx = int(val)
            except AssertionError as err:
                print format_error(err.message)
                val = ''
                continue

            if val_idx >= len(option_list):
                print format_error('Value is not an option')
                val = ''
                continue

        name, key = option_list[val_idx]
        return key


class InteractiveSelectionMenu(InteractiveSelectionNameValueMenu):
    def __init__(self, prompt, options):
        """
        Selection menu argument where you select a name and receive that name
        :param prompt: the prompt to ask the user
        :type prompt: str
        :param options: a list of options for the user to select
        :type options: list[str]
        """
        self.prompt = prompt
        self.options = options
        super(InteractiveSelectionMenu, self).__init__(prompt, map(lambda x: (x, x), self.options))

    def run(self, has_back=False):
        """
        Run the widget and return the name of the argument
        :param has_back: If the argument has the ability to go back (which returns None)
        :type has_back: bool
        :return: the value of the selected option
        :rtype: str | None
        """
        return super(InteractiveSelectionMenu, self).run(has_back=has_back)


class InteractiveSuperMenu(InteractiveSelectionNameValueMenu):
    def __init__(self, prompt, options):
        """
        Selection menu with sub selection menus that are run when you select one
        :param prompt: the prompt to ask the user
        :type prompt: str
        :param options: list of tuples containing the name of the option and function that it will run, which should
        either go back (None) or return the argument value
        :type options: list[(str, () -> (Any | None))]
        """
        self.prompt = prompt
        self.options = options
        super(InteractiveSuperMenu, self).__init__(prompt, options)

    def run(self, has_back=False):
        """
        Run the widget and return the value of the sub argument once one is run
        :param has_back: If the argument has the ability to go back (which returns None)
        :type has_back: bool
        :return: the value of the chosen option
        :rtype: Any | None
        """
        while True:
            ret = super(InteractiveSuperMenu, self).run(has_back=has_back)
            if ret is None:
                return None
            ret_called = ret()
            if ret_called is not None:
                return ret_called
