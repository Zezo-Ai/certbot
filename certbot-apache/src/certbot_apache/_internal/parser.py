"""ApacheParser is a member object of the ApacheConfigurator class."""
import copy
import fnmatch
import logging
import re
from typing import Collection
from typing import Dict
from typing import Iterable
from typing import List
from typing import Mapping
from typing import Optional
from typing import Set
from typing import Tuple
from typing import TYPE_CHECKING
from typing import Union

from certbot import errors
from certbot.compat import os
from certbot_apache._internal import apache_util
from certbot_apache._internal import constants

if TYPE_CHECKING:
    from certbot_apache._internal.configurator import ApacheConfigurator  # pragma: no cover

try:
    from augeas import Augeas
except ImportError:  # pragma: no cover
    Augeas = None

logger = logging.getLogger(__name__)


class ApacheParser:
    """Class handles the fine details of parsing the Apache Configuration.

    .. todo:: Make parsing general... remove sites-available etc...

    :ivar str root: Normalized absolute path to the server root
        directory. Without trailing slash.
    :ivar set modules: All module names that are currently enabled.
    :ivar dict loc: Location to place directives, root - configuration origin,
        default - user config file, name - NameVirtualHost,

    """
    arg_var_interpreter: re.Pattern[str] = re.compile(r"\$\{[^ \}]*}")
    fnmatch_chars: Set[str] = {"*", "?", "\\", "[", "]"}

    # pylint: disable=unused-argument
    def __init__(self, root: str, configurator: "ApacheConfigurator",
                 vhostroot: str, version: Tuple[int, ...] = (2, 4)) -> None:
        # Note: Order is important here.

        # Needed for calling save() with reverter functionality that resides in
        # AugeasConfigurator superclass of ApacheConfigurator. This resolves
        # issues with aug.load() after adding new files / defines to parse tree
        self.configurator = configurator

        # Initialize augeas
        self.aug: Augeas = init_augeas()

        if not self.check_aug_version():
            raise errors.NotSupportedError(
                "Apache plugin support requires libaugeas0 and augeas-lenses "
                "version 1.2.0 or higher, please make sure you have you have "
                "those installed.")

        self.modules: Dict[str, Optional[str]] = {}
        self.parser_paths: Dict[str, List[str]] = {}
        self.variables: Dict[str, str] = {}

        # Find configuration root and make sure augeas can parse it.
        self.root: str = os.path.abspath(root)
        self.loc: Dict[str, str] = {"root": self._find_config_root()}
        self.parse_file(self.loc["root"])

        # Look up variables from httpd and add to DOM if not already parsed
        self.update_runtime_variables()

        # This problem has been fixed in Augeas 1.0
        self.standardize_excl()

        # Parse LoadModule directives from configuration files
        self.parse_modules()

        # Set up rest of locations
        self.loc.update(self._set_locations())

        # list of the active include paths, before modifications
        self.existing_paths = copy.deepcopy(self.parser_paths)

        # Must also attempt to parse additional virtual host root
        if vhostroot:
            self.parse_file(os.path.abspath(vhostroot) + "/" +
                            self.configurator.options.vhost_files)

    def check_parsing_errors(self, lens: str) -> None:
        """Verify Augeas can parse all of the lens files.

        :param str lens: lens to check for errors

        :raises .errors.PluginError: If there has been an error in parsing with
            the specified lens.

        """
        error_files = self.aug.match("/augeas//error")

        for path in error_files:
            # Check to see if it was an error resulting from the use of
            # the httpd lens
            lens_path = self.aug.get(path + "/lens")
            # As aug.get may return null
            if lens_path and lens in lens_path:
                msg = (
                    "There has been an error in parsing the file {0} on line {1}: "
                    "{2}".format(
                    # Strip off /augeas/files and /error
                    path[13:len(path) - 6],
                    self.aug.get(path + "/line"),
                    self.aug.get(path + "/message")))
                raise errors.PluginError(msg)

    def check_aug_version(self) -> Union[bool, List[str]]:
        """ Checks that we have recent enough version of libaugeas.
        If augeas version is recent enough, it will support case insensitive
        regexp matching"""

        self.aug.set("/test/path/testing/arg", "aRgUMeNT")
        try:
            matches: List[str] = self.aug.match(
                "/test//*[self::arg=~regexp('argument', 'i')]")
        except RuntimeError:
            self.aug.remove("/test/path")
            return False
        self.aug.remove("/test/path")
        return matches

    def unsaved_files(self) -> Set[str]:
        """Lists files that have modified Augeas DOM but the changes have not
        been written to the filesystem yet, used by `self.save()` and
        ApacheConfigurator to check the file state.

        :raises .errors.PluginError: If there was an error in Augeas, in
            an attempt to save the configuration, or an error creating a
            checkpoint

        :returns: `set` of unsaved files
        """
        save_state = self.aug.get("/augeas/save")
        self.aug.set("/augeas/save", "noop")
        # Existing Errors
        ex_errs = self.aug.match("/augeas//error")
        try:
            # This is a noop save
            self.aug.save()
        except (OSError, RuntimeError):
            self._log_save_errors(ex_errs)
            # Erase Save Notes
            self.configurator.save_notes = ""
            raise errors.PluginError(
                "Error saving files, check logs for more info.")

        # Return the original save method
        self.aug.set("/augeas/save", save_state)

        # Retrieve list of modified files
        # Note: Noop saves can cause the file to be listed twice, I used a
        # set to remove this possibility. This is a known augeas 0.10 error.
        save_paths = self.aug.match("/augeas/events/saved")

        save_files = set()
        if save_paths:
            for path in save_paths:
                save_files.add(self.aug.get(path)[6:])
        return save_files

    def ensure_augeas_state(self) -> None:
        """Makes sure that all Augeas dom changes are written to files to avoid
        loss of configuration directives when doing additional augeas parsing,
        causing a possible augeas.load() resulting dom reset
        """

        if self.unsaved_files():
            self.configurator.save_notes += "(autosave)"
            self.configurator.save()

    def save(self, save_files: Iterable[str]) -> None:
        """Saves all changes to the configuration files.

        save() is called from ApacheConfigurator to handle the parser specific
        tasks of saving.

        :param list save_files: list of strings of file paths that we need to save.

        """
        self.configurator.save_notes = ""

        ex_errs = self.aug.match("/augeas//error")
        try:
            self.aug.save()
        except OSError:
            self._log_save_errors(ex_errs)
            raise

        # Force reload if files were modified
        # This is needed to recalculate augeas directive span
        if save_files:
            for sf in save_files:
                self.aug.remove("/files/"+sf)
            self.aug.load()

    def _log_save_errors(self, ex_errs: Iterable[str]) -> None:
        """Log errors due to bad Augeas save.

        :param list ex_errs: Existing errors before save

        """
        # Check for the root of save problems
        new_errs = [e for e in self.aug.match("/augeas//error") if e not in ex_errs]

        for err in new_errs:
            logger.debug(
                "Error %s saving %s: %s", self.aug.get(err), err[13:len(err) - 6],
                self.aug.get(f"{err}/message"))
        logger.error(
            "Unable to save files: %s.%s", ", ".join(err[13:len(err) - 6] for err in new_errs),
            f" Save Notes: {self.configurator.save_notes}" if self.configurator.save_notes else "")

    def add_include(self, main_config: str, inc_path: str) -> None:
        """Add Include for a new configuration file if one does not exist

        :param str main_config: file path to main Apache config file
        :param str inc_path: path of file to include

        """
        if not self.find_dir(case_i("Include"), inc_path):
            logger.debug("Adding Include %s to %s",
                         inc_path, get_aug_path(main_config))
            self.add_dir(
                get_aug_path(main_config),
                "Include", inc_path)

            # Add new path to parser paths
            new_dir = os.path.dirname(inc_path)
            new_file = os.path.basename(inc_path)
            self.existing_paths.setdefault(new_dir, []).append(new_file)

    def add_mod(self, mod_name: str) -> None:
        """Shortcut for updating parser modules."""
        if mod_name + "_module" not in self.modules:
            self.modules[mod_name + "_module"] = None
        if "mod_" + mod_name + ".c" not in self.modules:
            self.modules["mod_" + mod_name + ".c"] = None

    def reset_modules(self) -> None:
        """Reset the loaded modules list. This is called from cleanup to clear
        temporarily loaded modules."""
        self.modules = {}
        self.update_modules()
        self.parse_modules()

    def parse_modules(self) -> None:
        """Iterates on the configuration until no new modules are loaded.

        ..todo:: This should be attempted to be done with a binary to avoid
            the iteration issue.  Else... parse and enable mods at same time.

        """
        mods: Dict[str, str] = {}
        matches = self.find_dir("LoadModule")
        iterator = iter(matches)
        # Make sure prev_size != cur_size for do: while: iteration
        prev_size = -1

        while len(mods) != prev_size:
            prev_size = len(mods)

            for match_name, match_filename in zip(
                    iterator, iterator):
                mod_name = self.get_arg(match_name)
                mod_filename = self.get_arg(match_filename)
                if mod_name and mod_filename:
                    mods[mod_name] = mod_filename
                    mods[os.path.basename(mod_filename)[:-2] + "c"] = mod_filename
                else:
                    logger.debug("Could not read LoadModule directive from Augeas path: %s",
                                 match_name[6:])
        self.modules.update(mods)

    def update_runtime_variables(self) -> None:
        """Update Includes, Defines and Includes from httpd config dump data"""

        self.update_defines()
        self.update_includes()
        self.update_modules()

    def update_defines(self) -> None:
        """Updates the dictionary of known variables in the configuration"""
        self.variables = apache_util.parse_defines(self.configurator.options.get_defines_cmd)

    def update_includes(self) -> None:
        """Get includes from httpd process, and add them to DOM if needed"""

        # Find_dir iterates over configuration for Include and IncludeOptional
        # directives to make sure we see the full include tree present in the
        # configuration files
        _ = self.find_dir("Include")

        matches = apache_util.parse_includes(self.configurator.options.get_includes_cmd)
        if matches:
            for i in matches:
                if not self.parsed_in_current(i):
                    self.parse_file(i)

    def update_modules(self) -> None:
        """Get loaded modules from httpd process, and add them to DOM"""

        matches = apache_util.parse_modules(self.configurator.options.get_modules_cmd)
        for mod in matches:
            self.add_mod(mod.strip())

    def filter_args_num(self, matches: str, args: int) -> List[str]:
        """Filter out directives with specific number of arguments.

        This function makes the assumption that all related arguments are given
        in order.  Thus /files/apache/directive[5]/arg[2] must come immediately
        after /files/apache/directive[5]/arg[1]. Runs in 1 linear pass.

        :param str matches: Matches of all directives with arg nodes
        :param int args: Number of args you would like to filter

        :returns: List of directives that contain # of arguments.
            (arg is stripped off)

        """
        filtered: List[str] = []
        if args == 1:
            for i, match in enumerate(matches):
                if match.endswith("/arg"):
                    filtered.append(matches[i][:-4])
        else:
            for i, match in enumerate(matches):
                if match.endswith("/arg[%d]" % args):
                    # Make sure we don't cause an IndexError (end of list)
                    # Check to make sure arg + 1 doesn't exist
                    if (i == (len(matches) - 1) or
                            not matches[i + 1].endswith("/arg[%d]" %
                                                        (args + 1))):
                        filtered.append(matches[i][:-len("/arg[%d]" % args)])

        return filtered

    def add_dir_to_ifmodssl(self, aug_conf_path: str, directive: str, args: List[str]) -> None:
        """Adds directive and value to IfMod ssl block.

        Adds given directive and value along configuration path within
        an IfMod mod_ssl.c block.  If the IfMod block does not exist in
        the file, it is created.

        :param str aug_conf_path: Desired Augeas config path to add directive
        :param str directive: Directive you would like to add, e.g. Listen
        :param args: Values of the directive; list of str (eg. ["443"])
        :type args: list

        """
        # TODO: Add error checking code... does the path given even exist?
        #       Does it throw exceptions?
        if_mod_path = self.get_ifmod(aug_conf_path, "mod_ssl.c")
        # IfModule can have only one valid argument, so append after
        self.aug.insert(if_mod_path + "arg", "directive", False)
        nvh_path = if_mod_path + "directive[1]"
        self.aug.set(nvh_path, directive)
        if len(args) == 1:
            self.aug.set(nvh_path + "/arg", args[0])
        else:
            for i, arg in enumerate(args):
                self.aug.set("%s/arg[%d]" % (nvh_path, i + 1), arg)

    def get_ifmod(self, aug_conf_path: str, mod: str) -> str:
        """Returns the path to <IfMod mod> and creates one if it doesn't exist.

        :param str aug_conf_path: Augeas configuration path
        :param str mod: module ie. mod_ssl.c
        :param bool beginning: If the IfModule should be created to the beginning
            of augeas path DOM tree.

        :returns: Augeas path of the requested IfModule directive that pre-existed
            or was created during the process. The path may be dynamic,
            i.e. .../IfModule[last()]
        :rtype: str

        """
        if_mods: List[str] = self.aug.match(("%s/IfModule/*[self::arg='%s']" %
                                  (aug_conf_path, mod)))
        if not if_mods:
            return self.create_ifmod(aug_conf_path, mod)

        # Strip off "arg" at end of first ifmod path
        return if_mods[0].rpartition("arg")[0]

    def create_ifmod(self, aug_conf_path: str, mod: str) -> str:
        """Creates a new <IfMod mod> and returns its path.

        :param str aug_conf_path: Augeas configuration path
        :param str mod: module ie. mod_ssl.c

        :returns: Augeas path of the newly created IfModule directive.
            The path may be dynamic, i.e. .../IfModule[last()]
        :rtype: str

        """
        c_path = "{}/IfModule[last() + 1]".format(aug_conf_path)
        c_path_arg = "{}/IfModule[last()]/arg".format(aug_conf_path)
        self.aug.set(c_path, "")
        retpath = "{}/IfModule[last()]/".format(aug_conf_path)
        self.aug.set(c_path_arg, mod)
        return retpath

    def add_dir(
        self, aug_conf_path: Optional[str], directive: Optional[str], args: Union[List[str], str]
    ) -> None:
        """Appends directive to the end of the file given by aug_conf_path.

        .. note:: Not added to AugeasConfigurator because it may depend
            on the lens

        :param str aug_conf_path: Augeas configuration path to add directive
        :param str directive: Directive to add
        :param args: Value of the directive. ie. Listen 443, 443 is arg
        :type args: list or str

        """
        aug_conf_path = aug_conf_path if aug_conf_path else ""
        self.aug.set(aug_conf_path + "/directive[last() + 1]", directive)
        if isinstance(args, list):
            for i, value in enumerate(args, 1):
                self.aug.set(
                    "%s/directive[last()]/arg[%d]" % (aug_conf_path, i), value)
        else:
            self.aug.set(aug_conf_path + "/directive[last()]/arg", args)

    def add_dir_beginning(self, aug_conf_path: Optional[str], dirname: str,
                          args: Union[List[str], str]) -> None:
        """Adds the directive to the beginning of defined aug_conf_path.

        :param str aug_conf_path: Augeas configuration path to add directive
        :param str dirname: Directive to add
        :param args: Value of the directive. ie. Listen 443, 443 is arg
        :type args: list or str
        """
        aug_conf_path = aug_conf_path if aug_conf_path else ""
        first_dir = aug_conf_path + "/directive[1]"
        if self.aug.get(first_dir):
            self.aug.insert(first_dir, "directive", True)
        else:
            self.aug.set(first_dir, "directive")

        self.aug.set(first_dir, dirname)
        if isinstance(args, list):
            for i, value in enumerate(args, 1):
                self.aug.set(first_dir + "/arg[%d]" % (i), value)
        else:
            self.aug.set(first_dir + "/arg", args)

    def add_comment(self, aug_conf_path: str, comment: str) -> None:
        """Adds the comment to the augeas path

        :param str aug_conf_path: Augeas configuration path to add directive
        :param str comment: Comment content

        """
        self.aug.set(aug_conf_path + "/#comment[last() + 1]", comment)

    def find_comments(self, arg: str, start: Optional[str] = None) -> List[str]:
        """Finds a comment with specified content from the provided DOM path

        :param str arg: Comment content to search
        :param str start: Beginning Augeas path to begin looking

        :returns: List of augeas paths containing the comment content
        :rtype: list

        """
        if not start:
            start = get_aug_path(self.root)

        comments = self.aug.match("%s//*[label() = '#comment']" % start)

        results = []
        for comment in comments:
            c_content = self.aug.get(comment)
            if c_content and arg in c_content:
                results.append(comment)
        return results

    def find_dir(self, directive: str, arg: Optional[str] = None,
                 start: Optional[str] = None, exclude: bool = True) -> List[str]:
        """Finds directive in the configuration.

        Recursively searches through config files to find directives
        Directives should be in the form of a case insensitive regex currently

        .. todo:: arg should probably be a list
        .. todo:: arg search currently only supports direct matching. It does
            not handle the case of variables or quoted arguments. This should
            be adapted to use a generic search for the directive and then do a
            case-insensitive self.get_arg filter

        Note: Augeas is inherently case sensitive while Apache is case
        insensitive.  Augeas 1.0 allows case insensitive regexes like
        regexp(/Listen/, "i"), however the version currently supported
        by Ubuntu 0.10 does not.  Thus I have included my own case insensitive
        transformation by calling case_i() on everything to maintain
        compatibility.

        :param str directive: Directive to look for
        :param arg: Specific value directive must have, None if all should
                    be considered
        :type arg: str or None

        :param str start: Beginning Augeas path to begin looking
        :param bool exclude: Whether or not to exclude directives based on
            variables and enabled modules

        :rtype list

        """
        # Cannot place member variable in the definition of the function so...
        if not start:
            start = get_aug_path(self.loc["root"])

        # No regexp code
        # if arg is None:
        #     matches = self.aug.match(start +
        # "//*[self::directive='" + directive + "']/arg")
        # else:
        #     matches = self.aug.match(start +
        # "//*[self::directive='" + directive +
        #   "']/* [self::arg='" + arg + "']")

        # includes = self.aug.match(start +
        # "//* [self::directive='Include']/* [label()='arg']")

        regex = "(%s)|(%s)|(%s)" % (case_i(directive),
                                    case_i("Include"),
                                    case_i("IncludeOptional"))
        matches = self.aug.match(
            "%s//*[self::directive=~regexp('%s')]" % (start, regex))

        if exclude:
            matches = self.exclude_dirs(matches)

        if arg is None:
            arg_suffix = "/arg"
        else:
            arg_suffix = "/*[self::arg=~regexp('%s')]" % case_i(arg)

        ordered_matches: List[str] = []

        # TODO: Wildcards should be included in alphabetical order
        # https://httpd.apache.org/docs/2.4/mod/core.html#include
        for match in matches:
            dir_ = self.aug.get(match).lower()
            if dir_ in ("include", "includeoptional"):
                ordered_matches.extend(self.find_dir(
                    directive, arg,
                    self._get_include_path(self.get_arg(match + "/arg")),
                    exclude))
            # This additionally allows Include
            if dir_ == directive.lower():
                ordered_matches.extend(self.aug.match(match + arg_suffix))

        return ordered_matches

    def get_arg(self, match: str) -> Optional[str]:
        """Uses augeas.get to get argument value and interprets result.

        This also converts all variables and parameters appropriately.

        """
        value: str = self.aug.get(match)

        # No need to strip quotes for variables, as apache2ctl already does
        # this, but we do need to strip quotes for all normal arguments.

        # Note: normal argument may be a quoted variable
        # e.g. strip now, not later
        if not value:
            return None

        value = value.strip("'\"")

        variables = ApacheParser.arg_var_interpreter.findall(value)

        for var in variables:
            # Strip off ${ and }
            try:
                value = value.replace(var, self.variables[var[2:-1]])
            except KeyError:
                raise errors.PluginError("Error Parsing variable: %s" % var)

        return value

    def get_root_augpath(self) -> str:
        """
        Returns the Augeas path of root configuration.
        """
        return get_aug_path(self.loc["root"])

    def exclude_dirs(self, matches: Iterable[str]) -> List[str]:
        """Exclude directives that are not loaded into the configuration."""
        filters = [("ifmodule", self.modules.keys()), ("ifdefine", self.variables)]

        valid_matches = []

        for match in matches:
            for filter_ in filters:
                if not self._pass_filter(match, filter_):
                    break
            else:
                valid_matches.append(match)
        return valid_matches

    def _pass_filter(self, match: str, filter_: Tuple[str, Collection[str]]) -> bool:
        """Determine if directive passes a filter.

        :param str match: Augeas path
        :param list filter: list of tuples of form
            [("lowercase if directive", set of relevant parameters)]

        """
        match_l = match.lower()
        last_match_idx = match_l.find(filter_[0])

        while last_match_idx != -1:
            # Check args
            end_of_if = match_l.find("/", last_match_idx)
            # This should be aug.get (vars are not used e.g. parser.aug_get)
            expression = self.aug.get(match[:end_of_if] + "/arg")

            if expression.startswith("!"):
                # Strip off "!"
                if expression[1:] in filter_[1]:
                    return False
            else:
                if expression not in filter_[1]:
                    return False

            last_match_idx = match_l.find(filter_[0], end_of_if)

        return True

    def standard_path_from_server_root(self, arg: str) -> str:
        """Ensure paths are consistent and absolute

        :param str arg: Argument of directive

        :returns: Standardized argument path
        :rtype: str
        """
        # Remove beginning and ending quotes
        arg = arg.strip("'\"")

        # Standardize the include argument based on server root
        if not arg.startswith("/"):
            # Normpath will condense ../
            arg = os.path.normpath(os.path.join(self.root, arg))
        else:
            arg = os.path.normpath(arg)
        return arg

    def _get_include_path(self, arg: Optional[str]) -> Optional[str]:
        """Converts an Apache Include directive into Augeas path.

        Converts an Apache Include directive argument into an Augeas
        searchable path

        .. todo:: convert to use os.path.join()

        :param str arg: Argument of Include directive

        :returns: Augeas path string
        :rtype: str

        """
        # Check to make sure only expected characters are used <- maybe remove
        # validChars = re.compile("[a-zA-Z0-9.*?_-/]*")
        # matchObj = validChars.match(arg)
        # if matchObj.group() != arg:
        #     logger.error("Error: Invalid regexp characters in %s", arg)
        #     return []
        if arg is None:
            return None  # pragma: no cover
        arg = self.standard_path_from_server_root(arg)

        # Attempts to add a transform to the file if one does not already exist
        if os.path.isdir(arg):
            self.parse_file(os.path.join(arg, "*"))
        else:
            self.parse_file(arg)

        # Argument represents an fnmatch regular expression, convert it
        # Split up the path and convert each into an Augeas accepted regex
        # then reassemble
        split_arg = arg.split("/")
        for idx, split in enumerate(split_arg):
            if any(char in ApacheParser.fnmatch_chars for char in split):
                # Turn it into an augeas regex
                # TODO: Can this instead be an augeas glob instead of regex
                split_arg[idx] = ("* [label()=~regexp('%s')]" %
                                  self.fnmatch_to_re(split))
        # Reassemble the argument
        # Note: This also normalizes the argument /serverroot/ -> /serverroot
        arg = "/".join(split_arg)

        return get_aug_path(arg)

    def fnmatch_to_re(self, clean_fn_match: str) -> str:
        """Method converts Apache's basic fnmatch to regular expression.

        Assumption - Configs are assumed to be well-formed and only writable by
        privileged users.

        https://apr.apache.org/docs/apr/2.0/apr__fnmatch_8h_source.html

        :param str clean_fn_match: Apache style filename match, like globs

        :returns: regex suitable for augeas
        :rtype: str

        """
        # Since Python 3.6, it returns a different pattern like (?s:.*\.load)\Z
        return fnmatch.translate(clean_fn_match)[4:-3]  # pragma: no cover

    def parse_file(self, filepath: str) -> None:
        """Parse file with Augeas

        Checks to see if file_path is parsed by Augeas
        If filepath isn't parsed, the file is added and Augeas is reloaded

        :param str filepath: Apache config file path

        """
        use_new, remove_old = self._check_path_actions(filepath)
        # Ensure that we have the latest Augeas DOM state on disk before
        # calling aug.load() which reloads the state from disk
        self.ensure_augeas_state()
        # Test if augeas included file for Httpd.lens
        # Note: This works for augeas globs, ie. *.conf
        if use_new:
            inc_test = self.aug.match(
                "/augeas/load/Httpd['%s' =~ glob(incl)]" % filepath)
            if not inc_test:
                # Load up files
                # This doesn't seem to work on TravisCI
                # self.aug.add_transform("Httpd.lns", [filepath])
                if remove_old:
                    self._remove_httpd_transform(filepath)
                self._add_httpd_transform(filepath)
                self.aug.load()

    def parsed_in_current(self, filep: Optional[str]) -> bool:
        """Checks if the file path is parsed by current Augeas parser config
        ie. returns True if the file is found on a path that's found in live
        Augeas configuration.

        :param str filep: Path to match

        :returns: True if file is parsed in existing configuration tree
        :rtype: bool
        """
        if not filep:
            return False  # pragma: no cover
        return self._parsed_by_parser_paths(filep, self.parser_paths)

    def parsed_in_original(self, filep: Optional[str]) -> bool:
        """Checks if the file path is parsed by existing Apache config.
        ie. returns True if the file is found on a path that matches Include or
        IncludeOptional statement in the Apache configuration.

        :param str filep: Path to match

        :returns: True if file is parsed in existing configuration tree
        :rtype: bool
        """
        if not filep:
            return False  # pragma: no cover
        return self._parsed_by_parser_paths(filep, self.existing_paths)

    def _parsed_by_parser_paths(self, filep: str, paths: Mapping[str, List[str]]) -> bool:
        """Helper function that searches through provided paths and returns
        True if file path is found in the set"""
        for directory in paths:
            for filename in paths[directory]:
                if fnmatch.fnmatch(filep, os.path.join(directory, filename)):
                    return True
        return False

    def _check_path_actions(self, filepath: str) -> Tuple[bool, bool]:
        """Determine actions to take with a new augeas path

        This helper function will return a tuple that defines
        if we should try to append the new filepath to augeas
        parser paths, and / or remove the old one with more
        narrow matching.

        :param str filepath: filepath to check the actions for

        """

        try:
            new_file_match = os.path.basename(filepath)
            existing_matches = self.parser_paths[os.path.dirname(filepath)]
            if "*" in existing_matches:
                use_new = False
            else:
                use_new = True
            remove_old = new_file_match == "*"
        except KeyError:
            use_new = True
            remove_old = False
        return use_new, remove_old

    def _remove_httpd_transform(self, filepath: str) -> None:
        """Remove path from Augeas transform

        :param str filepath: filepath to remove
        """

        remove_basenames = self.parser_paths[os.path.dirname(filepath)]
        remove_dirname = os.path.dirname(filepath)
        for name in remove_basenames:
            remove_path = remove_dirname + "/" + name
            remove_inc = self.aug.match(
                "/augeas/load/Httpd/incl [. ='%s']" % remove_path)
            self.aug.remove(remove_inc[0])
        self.parser_paths.pop(remove_dirname)

    def _add_httpd_transform(self, incl: str) -> None:
        """Add a transform to Augeas.

        This function will correctly add a transform to augeas
        The existing augeas.add_transform in python doesn't seem to work for
        Travis CI as it loads in libaugeas.so.0.10.0

        :param str incl: filepath to include for transform

        """
        last_include: str = self.aug.match("/augeas/load/Httpd/incl [last()]")
        if last_include:
            # Insert a new node immediately after the last incl
            self.aug.insert(last_include[0], "incl", False)
            self.aug.set("/augeas/load/Httpd/incl[last()]", incl)
        # On first use... must load lens and add file to incl
        else:
            # Augeas uses base 1 indexing... insert at beginning...
            self.aug.set("/augeas/load/Httpd/lens", "Httpd.lns")
            self.aug.set("/augeas/load/Httpd/incl", incl)
        # Add included path to paths dictionary
        try:
            self.parser_paths[os.path.dirname(incl)].append(
                os.path.basename(incl))
        except KeyError:
            self.parser_paths[os.path.dirname(incl)] = [
                os.path.basename(incl)]

    def standardize_excl(self) -> None:
        """Standardize the excl arguments for the Httpd lens in Augeas.

        Note: Hack!
        Standardize the excl arguments for the Httpd lens in Augeas
        Servers sometimes give incorrect defaults
        Note: This problem should be fixed in Augeas 1.0.  Unfortunately,
        Augeas 0.10 appears to be the most popular version currently.

        """
        # attempt to protect against augeas error in 0.10.0 - ubuntu
        # *.augsave -> /*.augsave upon augeas.load()
        # Try to avoid bad httpd files
        # There has to be a better way... but after a day and a half of testing
        # I had no luck
        # This is a hack... work around... submit to augeas if still not fixed

        excl = ["*.augnew", "*.augsave", "*.dpkg-dist", "*.dpkg-bak",
                "*.dpkg-new", "*.dpkg-old", "*.rpmsave", "*.rpmnew",
                "*~",
                self.root + "/*.augsave",
                self.root + "/*~",
                self.root + "/*/*augsave",
                self.root + "/*/*~",
                self.root + "/*/*/*.augsave",
                self.root + "/*/*/*~"]

        for i, excluded in enumerate(excl, 1):
            self.aug.set("/augeas/load/Httpd/excl[%d]" % i, excluded)

        self.aug.load()

    def _set_locations(self) -> Dict[str, str]:
        """Set default location for directives.

        Locations are given as file_paths
        .. todo:: Make sure that files are included

        """
        default: str = self.loc["root"]

        temp: str = os.path.join(self.root, "ports.conf")
        if os.path.isfile(temp):
            listen = temp
            name = temp
        else:
            listen = default
            name = default

        return {"default": default, "listen": listen, "name": name}

    def _find_config_root(self) -> str:
        """Find the Apache Configuration Root file."""
        location = ["apache2.conf", "httpd.conf", "conf/httpd.conf"]
        for name in location:
            if os.path.isfile(os.path.join(self.root, name)):
                return os.path.join(self.root, name)
        raise errors.NoInstallationError("Could not find configuration root")


def case_i(string: str) -> str:
    """Returns case insensitive regex.

    Returns a sloppy, but necessary version of a case insensitive regex.
    Any string should be able to be submitted and the string is
    escaped and then made case insensitive.
    May be replaced by a more proper /i once augeas 1.0 is widely
    supported.

    :param str string: string to make case i regex

    """
    return "".join("[" + c.upper() + c.lower() + "]"
                    if c.isalpha() else c for c in re.escape(string))


def get_aug_path(file_path: str) -> str:
    """Return augeas path for full filepath.

    :param str file_path: Full filepath

    """
    return "/files%s" % file_path


def init_augeas() -> Augeas:
    """ Initialize the actual Augeas instance """

    if not Augeas:  # pragma: no cover
        raise errors.NoInstallationError("Problem in Augeas installation")

    return Augeas(
        # specify a directory to load our preferred lens from
        loadpath=constants.AUGEAS_LENS_DIR,
        # Do not save backup (we do it ourselves), do not load
        # anything by default
        flags=(Augeas.NONE |
               Augeas.NO_MODL_AUTOLOAD |
               Augeas.ENABLE_SPAN))
