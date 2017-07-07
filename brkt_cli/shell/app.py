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
import os
import sys

import subprocess
from argparse import Namespace

from prompt_toolkit import Application, AbortAction, CommandLineInterface, filters
from prompt_toolkit.buffer import Buffer, AcceptAction
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import IsDone, Always, RendererHeightIsKnown
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding.manager import KeyBindingManager
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, ConditionalContainer, Window, FillControl, TokenListControl
from prompt_toolkit.layout.dimension import LayoutDimension
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.shortcuts import create_prompt_layout, create_eventloop
from pygments.token import Token

from brkt_cli.shell.enum import Enum
from brkt_cli.shell.get_set_inner_commmands import set_inner_command, get_inner_command
from brkt_cli.shell.inner_commands import exit_inner_command, manpage_inner_command, InnerCommandError, \
    help_inner_command, dev_inner_command, editing_mode_inner_command
from brkt_cli.shell.manpage import Manpage


class App(object):
    """
    :type COMMAND_PREFIX: unicode
    :type INNER_COMMANDS: dict[unicode, brkt_cli.shell.inner_commands.InnerCommand]
    :type completer: brkt_cli.shell.completer.ShellCompleter
    :type cmd: brkt_cli.shell.raw_commands.CommandPromptToolkit
    :type has_manpage: bool
    :type key_manager: prompt_toolkit.key_binding.manager.KeyBindingManager
    :type dummy: bool
    :type set_args: dict[unicode, dict[unicode, Any]]
    :type saved_commands: dict[unicode, unicode]
    :type _vi_mode: bool
    :type _cli: prompt_toolkit.CommandLineInterface
    """

    class MachineCommands(Enum):
        """
        An enumeration of the machine commands that are returned after a inner command
        :type Unknown: int
        :type Exit: int
        """
        Unknown, Exit = range(2)

    COMMAND_PREFIX = u'/'
    INNER_COMMANDS = {
        COMMAND_PREFIX + u'exit': exit_inner_command,
        COMMAND_PREFIX + u'manpage': manpage_inner_command,
        COMMAND_PREFIX + u'help': help_inner_command,
        COMMAND_PREFIX + u'set': set_inner_command,
        COMMAND_PREFIX + u'get': get_inner_command,
        COMMAND_PREFIX + u'dev': dev_inner_command,
        COMMAND_PREFIX + u'editing_mode': editing_mode_inner_command,
    }

    def __init__(self, completer, cmd):
        """
        The app that controls the console UI and anything relating to the shell.
        :param completer: the completer to suggest words
        :type completer: brkt_cli.shell.completer.ShellCompleter
        :param cmd: the top command of the app.
        :type cmd: brkt_cli.shell.raw_commands.CommandPromptToolkit
        """
        self.completer = completer
        self.completer.app = self
        self.cmd = cmd
        self.has_manpage = True
        self.key_manager = None
        self.dummy = False
        self.set_args = {}
        self.saved_commands = {}
        self._vi_mode = False  # Determines if the prompt will run with emacs keybindings or vi ones

        self._cli = self.make_cli_interface()

    def run(self):
        """
        Runs the app in a while True loop. The app can only be broken from an error or EXIT machine_command. It catches
        all inner commands and runs them. At the end of a command, it clears the manpage screen.
        """
        while True:
            try:
                ret_doc = self._cli.run(reset_current_buffer=True)
            except (KeyboardInterrupt, EOFError):
                self._cli.eventloop.close()
                break
            else:
                if ret_doc is self.MachineCommands.Exit:
                    self._cli.eventloop.close()
                    break
                if ret_doc.text == '':
                    continue
                if ret_doc.text.startswith(self.COMMAND_PREFIX):
                    try:
                        inner_cmd = self.INNER_COMMANDS[ret_doc.text.split()[0]]
                    except KeyError:
                        print "Error: Unknown command."
                    else:
                        try:
                            machine_cmd = inner_cmd.run_action(ret_doc.text, self)
                            if machine_cmd == self.MachineCommands.Exit:
                                self._cli.eventloop.close()
                                break
                        except InnerCommandError as err:
                            print err.format_error()
                        except AssertionError as err:
                            print InnerCommandError.format(err.message)
                        except:
                            print InnerCommandError.format("unknown error - %s" % sys.exc_info()[0])
                    continue

                command = self.completer.get_current_command(ret_doc, True)
                cmd_text = ret_doc.text

                if command is not None and command.path in self.set_args:
                    args_text = ret_doc.text[self.completer.get_current_command_location(ret_doc)[1]:].strip()
                    try:
                        existing_args = command.parse_args(args_text.split())
                    except:
                        existing_args = Namespace()
                    set_args_dict = self.set_args[command.path]
                    positional_idx = 0
                    final_args = []
                    final_arg_texts = []
                    for arg in command.optional_arguments + command.positionals:
                        new_arg = {
                            'positional': None,
                            'arg': arg,
                            'value': None,
                            'specified': False,
                        }
                        if arg.type == arg.Type.Help or arg.type == arg.Type.Version:
                            continue

                        if arg.__class__.__name__ == 'PositionalArgumentPromptToolkit':
                            new_arg['positional'] = positional_idx
                            positional_idx += 1

                        if hasattr(existing_args, arg.dest):
                            spec_arg_val = getattr(existing_args, arg.dest)

                            if arg.default == spec_arg_val and spec_arg_val is not None:
                                if new_arg['positional'] is None:
                                    new_arg['specified'] = arg.tag in args_text
                                else:
                                    new_arg['specified'] = spec_arg_val in args_text
                            else:
                                new_arg['specified'] = True
                            new_arg['value'] = spec_arg_val
                        if arg.get_name() in set_args_dict and (not (
                            hasattr(existing_args, arg.dest) and getattr(existing_args, arg.dest) is not None) or not
                                new_arg['specified']):
                            new_arg['value'] = set_args_dict[arg.get_name()]
                            new_arg['specified'] = True

                        if new_arg['value'] is not None and new_arg['specified']:
                            final_args.append(new_arg)
                            
                    # FIXME: THIS DOESNT WORK WITH APPEND CONST

                    for final_opt_arg in filter(lambda x: x['positional'] is None, final_args):
                        arg = final_opt_arg['arg']
                        if arg.type == arg.Type.Store:
                            final_arg_texts.append(arg.tag + ' ' + str(final_opt_arg['value']))
                        elif arg.type == arg.Type.StoreConst and final_opt_arg['value'] is True:
                            final_arg_texts.append(arg.tag)
                        elif arg.type == arg.Type.StoreFalse and final_opt_arg['value'] is False:
                            final_arg_texts.append(arg.tag)
                        elif arg.type == arg.Type.StoreTrue and final_opt_arg['value'] is True:
                            final_arg_texts.append(arg.tag)
                        elif arg.type == arg.Type.Append:
                            final_arg_texts.append(
                                ' '.join(map(lambda val: arg.tag + ' ' + val, final_opt_arg['value'])))
                        elif arg.type == arg.Type.AppendConst or arg.type == arg.Type.Count:
                            final_arg_texts.append(' '.join(map(lambda val: arg.tag, final_opt_arg['value'])))

                    final_arg_texts.extend(map(lambda x: x['value'],
                                               sorted(filter(lambda x: x['positional'] is not None, final_args),
                                                      key=lambda x: x['positional'])))

                    cmd_text = ret_doc.text[:self.completer.get_current_command_location(ret_doc)[1]] + ' ' + ' '.join(
                        final_arg_texts)

                if self.dummy:
                    print sys.argv[0] + ' ' + cmd_text
                else:
                    p = subprocess.Popen(sys.argv[0] + ' ' + cmd_text, shell=True, env=os.environ.copy())
                    p.communicate()

    def get_bottom_toolbar_tokens(self, _):
        """
        Constructs the bottom toolbar
        :param _:
        :return: A list of all elements in the toolbar
        :rtype: list[(pygments.token._TokenType, unicode)]
        """
        ret = [
            (Token.Toolbar.Help, 'Press ctrl q to quit'),
            (Token.Toolbar.Separator, ' | '),
            (Token.Toolbar.Help, 'Manpage Window: ' + ('ON' if self.has_manpage else 'OFF')),
            (Token.Toolbar.Separator, ' | '),
            (Token.Toolbar.Help, 'Editing Mode: ' + ('VI' if self._vi_mode else 'Emacs')),
        ]
        if self.dummy:
            ret.extend([(Token.Toolbar.Separator, ' | '), (Token.Toolbar.Help, 'Dummy Mode: ON')])
        return ret

    def make_layout(self):
        """
        Constructs the layout of the CLI UI
        :return: The layout in horizontal sections
        :rtype: prompt_toolkit.layout.HSplit
        """
        return HSplit([
            create_prompt_layout(
                message=u'brkt> ',
                reserve_space_for_menu=8,
                wrap_lines=True,

            ),  # The command prompt
            ConditionalContainer(
                content=Window(height=LayoutDimension.exact(1),
                               content=FillControl(u'\u2500',
                                                   token=Token.Separator)),
                filter=~IsDone() & filters.Condition(
                    lambda _: self.has_manpage and self._cli.current_buffer.document.text != ''),
            ),  # A separator between the command prompt and the manpage view. This disappears when
            # self.has_manpage is False
            ConditionalContainer(
                content=Window(
                    content=Manpage(self),
                    height=LayoutDimension(max=15),
                ),
                filter=~IsDone() & filters.Condition(
                    lambda _: self.has_manpage and self._cli.current_buffer.document.text != ''),
            ),
            ConditionalContainer(
                content=Window(
                    TokenListControl(
                        self.get_bottom_toolbar_tokens,
                        default_char=Char(' ', Token.Toolbar)
                    ),
                    height=LayoutDimension.exact(1)
                ),
                filter=~IsDone() & RendererHeightIsKnown()
            )  # The bottom toolbar, which displays useful info to the user
        ])

    @staticmethod
    def make_buffer(completer):
        """
        This function makes a buffer for the app to use
        :param completer: the completer to suggest words
        :type completer: prompt_toolkit.completion.Completer
        :return: the generated buffer
        :rtype: prompt_toolkit.buffer.Buffer
        """
        return Buffer(
            enable_history_search=True,  # Allows the user to search through command history via the up and down arrows
            completer=completer,  # The completer to suggest words to the user
            complete_while_typing=Always(),  # Always give suggestions while typing
            accept_action=AcceptAction.RETURN_DOCUMENT,  # Return the document (and the text) on enter
            history=FileHistory('.brkt_cli_history')
        )

    def make_app(self, completer):
        """
        Makes the application needed for the App. Creates a KeyBindingManager that traps key commands and pipes them to
        functions.
        :param completer: the completer to suggest words
        :type completer: prompt_toolkit.completion.Completer
        :return: Application
        :rtype: prompt_toolkit.Application
        """
        self.key_manager = KeyBindingManager()

        @self.key_manager.registry.add_binding(Keys.ControlQ, eager=True)
        def exit_(event):
            """
            When ctrl q is pressed, return the EXIT machine_command to the cli.run() command.
            :param event:
            """
            event.cli.set_return_value(self.MachineCommands.Exit)

        return Application(
            buffer=self.make_buffer(completer),
            key_bindings_registry=self.key_manager.registry,
            on_abort=AbortAction.RETRY,
            layout=self.make_layout(),
            editing_mode=EditingMode.VI if self._vi_mode else EditingMode.EMACS,
            mouse_support=True,
        )

    @property
    def vi_mode(self):
        return self._vi_mode

    @vi_mode.setter
    def vi_mode(self, value):
        self._vi_mode = value
        if self._vi_mode:
            self._cli.application.editing_mode = EditingMode.VI
            self._cli.editing_mode = EditingMode.VI
        else:
            self._cli.application.editing_mode = EditingMode.EMACS
            self._cli.editing_mode = EditingMode.EMACS
        self._cli.request_redraw()

    def make_cli_interface(self):
        """
        Makes the CLI interface needed for the App
        :return: command line interface
        :rtype: prompt_toolkit.CommandLineInterface
        """
        loop = create_eventloop()
        app = self.make_app(self.completer)

        return CommandLineInterface(application=app, eventloop=loop)
