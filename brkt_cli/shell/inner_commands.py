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
from termcolor import colored


class InnerCommand:
    """
    :type name: unicode
    :type description: unicode
    :type usage: unicode
    :type _action: (list[unicode], brkt_cli.shell.app.App) -> (int | None)
    :type completer: (int, brkt_cli.shell.app.App, list[unicode], prompt_toolkit.document.Document) -> list[unicode]
    """
    def __init__(self, name, description, usage, action):
        """
        The command type for commands in the shell itself (e.g. `/exit`)
        :param name: the name of the command without the identifier (in this case `/`) in front of it
        :type name: unicode
        :param description: the description of the command
        :type description: unicode
        :param usage: the usage information of the command
        :type usage: unicode
        :param action: the action to be run when the command is entered.
        :type action: (list[unicode], brkt_cli.shell.app.App) -> (int | None)
        """
        self.name = name
        self.description = description
        self.usage = usage
        self._action = action
        self.completer = inner_command_completer_static(completions=[])

    def run_action(self, cmd, app):
        """
        Run the action of the inner command
        :param cmd: the entire command text
        :type cmd: unicode
        :param app: the app it is running from
        :type app: brkt_cli.shell.app.App
        :return: the result of the action
        :rtype: brkt_cli.shell.app.App.MachineCommands | None
        """
        params = cmd.split()
        del params[0]
        return self._action(params, app)


class InnerCommandError(Exception):
    def __init__(self, message):
        """
        An error that an inner command can throw.
        :param message: the error message
        :type message: unicode
        """
        super(InnerCommandError, self).__init__(message)
        pass

    @classmethod
    def format(cls, message):
        """
        Formats the error to be displayed in the CLI
        :param message: the error message
        :type message: unicode
        :return: the formatted error
        :rtype: unicode
        """
        return 'Error: %s' % message

    def format_error(self):
        """
        Formats the error to be displayed in the CLI
        :return: the formatted error
        :rtype: unicode
        """
        return self.format(self.message)


def inner_command_completer_static(completions=None):
    """
    Generate a command completer with static values
    :param completions: a list of a list of possible values for each argument. The possible values in the first
    argument would be a list of unicode strings as the first element in the top list. For example, completions[0] would
    get the fist set of values
    :type completions: list[list[unicode]]
    :return: a list of possible values to suggest
    :rtype: (int, brkt_cli.shell.app.App, list[unicode], prompt_toolkit.document.Document) -> list[unicode]
    """
    if completions is None:
        completions = []

    def complete(arg_idx, app, full_args_text, document):
        """
        The completer function
        :param arg_idx: the index of the current and selected argument
        :type arg_idx: int
        :param app: the app that is running
        :type app: brkt_cli.shell.app.App
        :param full_args_text: the text arguments that are finished
        :type full_args_text: list[unicode]
        :param document: the document of the current prompt/buffer
        :type document: prompt_toolkit.document.Document
        :return: a list of acceptable suggestions
        :rtype: list[unicode]
        """
        if arg_idx >= len(completions):
            return []
        return completions[arg_idx]

    return complete


def exit_inner_command_func(params, app):
    """
    Run exit command
    :param params: command parameters
    :type params: list[unicode]
    :param app: the app it is running from
    :type app: brkt_cli.shell.app.App
    :return: the exit machine_command
    :rtype: int
    :raises: AssertionError
    """
    assert len(params) == 0
    return app.MachineCommands.Exit


def manpage_inner_command_func(params, app):
    """
    Modify the app.has_manpage field depending on the parameters. If there are no parameters, toggle the field. If a
    parameter is specified and is either 'true or 'false', set it to that. If it isn't, throw error
    :param params: command parameters
    :type params: list[unicode]
    :param app: the app it is running from
    :type app: brkt_cli.shell.app.App
    :return: nothing
    :rtype: None
    :raises: AssertionError
    :raises: InnerCommandError
    """
    assert len(params) <= 1
    if params and len(params) > 0:
        if params[0].lower() == 'true':
            app.has_manpage = True
        elif params[0].lower() == 'false':
            app.has_manpage = False
        else:
            raise InnerCommandError('Unknown option entered')
    else:
        app.has_manpage = not app.has_manpage


def help_inner_command_func(params, app):
    """
    Prints help for the inner commands
    :param params: command parameters
    :type params: list[unicode]
    :param app: the app it is running from
    :type app: brkt_cli.shell.app.App
    :return: nothing
    :rtype: None
    :raises: AssertionError
    """
    assert len(params) == 0
    print colored('Brkt CLI Shell Inner Command Help', attrs=['bold'])
    for _, cmd in app.INNER_COMMANDS.iteritems():
        print cmd.name + '\t' + cmd.description
        print '\t' + app.COMMAND_PREFIX + cmd.usage


def dev_inner_command_func(params, app):
    """
    Under the hood developer tools to aid developers
    :param params: command parameters
    :type params: list[unicode]
    :param app: the app it is running from
    :type app: brkt_cli.shell.app.App
    :return: nothing
    :rtype: None
    :raises: AssertionError
    :raises: InnerCommandError
    """
    assert len(params) >= 1
    if params[0] == 'list_args':
        assert len(params) >= 2
        got_cmd = app.cmd.get_subcommand_from_path(params[1])
        if got_cmd is None:
            raise InnerCommandError('Unknown path subcommand')
        for arg in got_cmd.optional_arguments+got_cmd.positionals:
            print arg.raw

# Commands that are prebuilt for the CLI
exit_inner_command = InnerCommand('exit', 'Exits the shell.', 'exit', exit_inner_command_func)
manpage_inner_command = InnerCommand('manpage', 'Passing "true" will enable the manpage, while "false" will disable '
                                                'it. Passing nothing will toggle it.', 'manpage [true | false]',
                                     manpage_inner_command_func)
manpage_inner_command.completer = inner_command_completer_static([['true', 'false']])
help_inner_command = InnerCommand('help', 'Get help for inner commands', 'help', help_inner_command_func)
dev_inner_command = InnerCommand('dev', 'Under the hood access for developers', 'dev', dev_inner_command_func)
