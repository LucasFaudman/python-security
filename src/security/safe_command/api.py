import subprocess
import shlex
from re import compile as re_compile
from pathlib import Path
from glob import iglob
from os import getenv, get_exec_path, access, X_OK
from os.path import expanduser, expandvars
from shutil import which
from typing import Union, Optional, Dict, List, Tuple, Set, FrozenSet, Sequence, Callable, Iterator, Any
from security.exceptions import SecurityException

if subprocess._mswindows: # type: ignore
    from warnings import warn
    warn(RuntimeWarning("SafeCommand not yet fully supported on Windows. Only use if you know what you are doing."))

ValidConfigVal = Union[FrozenSet[str], Set[str]]
ValidConfig = Dict[str, ValidConfigVal]
ValidRestrictions = Optional[Union[ValidConfigVal , Sequence[str]]]
ValidCommand = Union[str, List[str]]

DEFAULT_RESTRICTIONS = frozenset(
    ("PREVENT_COMMAND_CHAINING",
     "PREVENT_ARGUMENTS_TARGETING_SENSITIVE_FILES",
     "PREVENT_COMMON_EXPLOIT_EXECUTABLES",
     )
)

DEFAULT_SENSITIVE_FILE_PATHS = frozenset(
    (
        "/etc/passwd",
        "/etc/shadow",
        "/etc/group",
        "/etc/gshadow",
        "/etc/sysconfig/network",
        "/etc/network/interfaces",
        "/etc/resolv.conf",
        "/etc/sudoers",
        "/etc/hosts",
    )
)

DEFAULT_BANNED_COMMON_EXPLOIT_EXECUTABLES = frozenset(
    ("nc", "netcat", "ncat", "curl", "wget", "dpkg", "rpm"))
DEFAULT_BANNED_PATHTYPES = frozenset(
    ("mount", "symlink", "block_device", "char_device", "fifo", "socket"))
DEFAULT_BANNED_OWNERS = frozenset(("root", "admin", "wheel", "sudo"))
DEFAULT_BANNED_GROUPS = DEFAULT_BANNED_OWNERS
DEFAULT_BANNED_COMMAND_CHAINING_SEPARATORS = frozenset(("&", ";", "|", "\n"))
DEFAULT_BANNED_COMMAND_AND_PROCESS_SUBSTITUTION_OPERATORS = frozenset(
    ("$(", "`", "<(", ">("))
DEFAULT_BANNED_COMMAND_CHAINING_EXECUTABLES = frozenset((
    "eval", "exec", "-exec", "env", "source", "sudo", "su", "gosu", "sudoedit",
    "xargs", "awk", "perl", "python", "ruby", "php", "lua", "sqlplus",
    "expect", "screen", "tmux", "byobu", "byobu-ugraph", "time",
    "nohup", "at", "batch", "anacron", "cron", "crontab", "systemctl", "service", "init", "telinit",
    "systemd", "systemd-run"
))
DEFAULT_SHELLS = frozenset(("sh", "bash", "zsh", "csh", "rsh", "tcsh", "tclsh", "ksh", "dash", "ash",
                            "jsh", "jcsh", "mksh", "wsh", "fish", "busybox", "powershell", "pwsh", "pwsh-preview", "pwsh-lts"))

DEFAULT_ALLOWED_SHELL_EXPANSION_OPERATORS = frozenset(('-', '=', '?', '+'))
DEFAULT_BANNED_SHELL_EXPANSION_OPERATORS = frozenset(
    ("!", "*", "@", "#", "%", "/", "^", ","))


def kwargs2config(kwargs: dict) -> ValidConfig:
    """
    Convert the kwargs to a config dict to be used by check().
    Removes all SafeCommand kwargs from kwargs so kwargs can be passed to Popen.
    """

    def make_set(val: Union[Sequence[str], str, None]) -> ValidConfigVal:
        if isinstance(val, (set, frozenset)): return val
        elif isinstance(val, str): return {val}
        elif isinstance(val, Sequence): return set(val)
        elif val is None: return set()
        else:
            raise TypeError(f"Invalid type {type(val).__name__!r} cannot be used as a SafeCommand config value. Must be [Set[str]|Sequence[str]|str|None]")


    # Config defaults to all DEFAULT_ prefixed variables in the globals
    prefix = "DEFAULT_"
    kslice = slice(len(prefix), None)
    config = {k[kslice]: v for k, v in globals().items() if k.startswith(prefix)}
    
    # If the config kwarg is set update the config with it
    if (config_kwarg := kwargs.pop("config", None)):
        config.update(config_kwarg)

    if "restrictions" in kwargs:
        # restrictions kwarg takes precedence over the RESTRICTIONS config value
        config["RESTRICTIONS"] = make_set(kwargs.pop("restrictions"))

    for k in config:
        kwarg_ending = k.lower()
        if (set_vals := kwargs.pop(f"set_{kwarg_ending}", kwargs.pop(kwarg_ending, None))):
            # set_<config_key>=vals or <config_key>=vals can be used as kwargs to set the config values
            config[k] = make_set(set_vals)
        
        elif (add_vals := kwargs.pop(f"add_{kwarg_ending}", None)):
            # add_<config_key>=vals can be used as kwargs to add to the config values
            config[k] |= make_set(add_vals)

        elif (remove_vals := kwargs.pop(f"remove_{kwarg_ending}", kwargs.pop(kwarg_ending.replace("banned_", "allow_"), None))):
            # remove_<config_key>=vals or allow_<config_key_without_banned_prefix> can be used as kwargs to remove from the config values
            config[k] -= make_set(remove_vals)

    if (global_allow := kwargs.pop("allow", None)):
        # allow=vals can be used to explicitly allow any string(s)
        global_allow = make_set(global_allow)
        for k in config:
            config[k] -= global_allow
    
    return config


def _check_then_call(original_func: Callable, command: ValidCommand, *args, force_shell=False, **kwargs) -> Any:
    # If there is a command and it passes the checks pass it the original function call
    config = kwargs2config(kwargs)
    check(command, config, Popen_kwargs=kwargs, force_shell=force_shell)
    return _call_original(original_func, command, *args, **kwargs)

def _call_original(original_func: Callable, command: ValidCommand, *args, **kwargs) -> Any:
    return original_func(command, *args, **kwargs)


# Subprocess method wrappers
def run(command: ValidCommand, *args, restrictions: ValidRestrictions = DEFAULT_RESTRICTIONS, **kwargs) -> subprocess.CompletedProcess:
    return _check_then_call(subprocess.run, command, *args, restrictions=restrictions, **kwargs)

def call(command: ValidCommand, *args, restrictions: ValidRestrictions = DEFAULT_RESTRICTIONS, **kwargs) -> int:
    return _check_then_call(subprocess.call, command, *args, restrictions=restrictions, **kwargs)

def check_call(command: ValidCommand, *args, restrictions: ValidRestrictions = DEFAULT_RESTRICTIONS, **kwargs) -> int:
    return _check_then_call(subprocess.check_call, command, *args, restrictions=restrictions, **kwargs)

def getstatusoutput(command: ValidCommand, *args, restrictions: ValidRestrictions = DEFAULT_RESTRICTIONS, **kwargs) -> Tuple[int, str]:
    return _check_then_call(subprocess.getstatusoutput, command, *args, restrictions=restrictions, force_shell=True, **kwargs)

def getoutput(command: ValidCommand, *args, restrictions: ValidRestrictions = DEFAULT_RESTRICTIONS, **kwargs) -> str:
    return _check_then_call(subprocess.getoutput, command, *args, restrictions=restrictions, force_shell=True, **kwargs)

def check_output(command: ValidCommand, *args, restrictions: ValidRestrictions = DEFAULT_RESTRICTIONS, **kwargs) -> str:
    return _check_then_call(subprocess.check_output, command, *args, restrictions=restrictions, **kwargs)

class Popen(subprocess.Popen):
    def __init__(self, command: ValidCommand, *args, restrictions: ValidRestrictions = DEFAULT_RESTRICTIONS, **kwargs):
        _check_then_call(super().__init__, command, *args, restrictions=restrictions, **kwargs)


# SafeCommandRunner/SafeCommand class
class SafeCommandRunner:
    def __init__(self, restrictions: ValidRestrictions = DEFAULT_RESTRICTIONS, **kwargs) -> None:
        self.config = kwargs2config({"restrictions": restrictions, **kwargs})
        self.restrictions = self.config["RESTRICTIONS"]

    def update_config(self, **kwargs) -> None:
        keys_to_update = set(kwargs.keys()) # Store the keys before they are popped by kwargs2config
        new_config = kwargs2config(kwargs)
        for k in keys_to_update:
            config_key = k.split("_", 1)[1].upper() if "_" in k else k.upper()
            if config_key in self.config:
                new_val = new_config[config_key]
                self.config[config_key] = new_val
                if config_key == "RESTRICTIONS":
                    self.restrictions = new_val
            elif config_key != 'ALLOW':
                raise ValueError(f"Invalid SafeCommand config key: {k}")

    def update_restrictions(self, restrictions: ValidRestrictions) -> None:
        self.update_config(restrictions=restrictions)

    def run(self, command: ValidCommand, *args, **kwargs) -> subprocess.CompletedProcess:
        return run(command, *args, restrictions=self.restrictions, config=self.config, **kwargs)
    
    def call(self, command: ValidCommand, *args, **kwargs) -> int:
        return call(command, *args, restrictions=self.restrictions, config=self.config, **kwargs)
    
    def check_call(self, command: ValidCommand, *args, **kwargs) -> int:
        return check_call(command, *args, restrictions=self.restrictions, config=self.config, **kwargs)
    
    def getstatusoutput(self, command: ValidCommand, *args, **kwargs) -> Tuple[int, str]:
        return getstatusoutput(command, *args, restrictions=self.restrictions, config=self.config, **kwargs)
    
    def getoutput(self, command: ValidCommand, *args, **kwargs) -> str:
        return getoutput(command, *args, restrictions=self.restrictions, config=self.config, **kwargs)
    
    def check_output(self, command: ValidCommand, *args, **kwargs) -> str:
        return check_output(command, *args, restrictions=self.restrictions, config=self.config, **kwargs)
    
    def Popen(self, command: ValidCommand, *args, **kwargs) -> subprocess.Popen:
        return Popen(command, *args, restrictions=self.restrictions, config=self.config, **kwargs)
    
    def __str__(self) -> str:
        return f"""SafeCommandRunner({", ".join(f'{k}=[{", ".join(v) if v else "NONE"}]' for k, v in self.config.items())})"""

SafeCommand = SafeCommandRunner # Alias for SafeCommandRunner


# Shell expansion and command parsing functions
def _get_env_var_value(var: str, venv: Optional[dict] = None, default: Optional[str] = None) -> str:
    """
    Try to get the value of the environment variable var.
    First check the venv if it is provided and the variable is set. 
    then check for a value with os.getenv then with os.path.expandvars.
    Returns an empty string if the variable is not set.
    """

    # Use the venv if it is provided and the variable is set, even when it is an empty string
    if venv and (value := venv.get(var)) is not None:
        return value

    # Try os.getenv first
    if (value := getenv(var)):
        return value

    if not var.startswith("$"):
        var = f"${var}"  # expandvars takes a var in form $var or ${var}
    # Try os.path.expandvars
    if (value := expandvars(var)) != var:
        return value
    else:
        return default or ""


def _strip_quotes(string: str) -> str:
    """
    Strips either type of quotes but not both
    """
    if string.startswith("'") and string.endswith("'"):
        return string.strip("'")
    elif string.startswith('"') and string.endswith('"'):
        return string.strip('"')
    else:
        return string


def _replace_all(string: str, replacements: dict, reverse=False) -> str:
    for old, new in replacements.items():
        if reverse:
            string = string.replace(new, old)
        else:
            string = string.replace(old, new)
    return string


def _simple_shell_math(expression: Union[str, Iterator[str]], venv: dict, operator: str = '+') -> int:
    """
    Handles arithmetic expansion of bracket paramters like ${HOME:1+1:5-2} == ${HOME:2:3}
    Only supports + - for now since * / % are banned shell expansion operators
    venv is used since env vars can be set or modified while evaluating the arithmetic expansion

    Implementation is based on Bash shell arithmetic rules:
    https://www.gnu.org/software/bash/manual/html_node/Shell-Arithmetic.html
    """

    ALLOWED_OPERATORS = "+-"

    def is_valid_shell_number(string: str) -> bool:
        return string.lstrip('+-').replace(".", "", 1).isnumeric()

    def is_operator(char: str) -> bool:
        return char in ALLOWED_OPERATORS

    def is_assignment_operator(char: str) -> bool:
        return char == "="

    def evaluate_stack(stack: list, venv: dict) -> float:
        if not stack:
            return 0

        # Join items in the stack to form a string for evaluation
        stack_str = ''.join(stack)

        # If the stack is a number return it
        if is_valid_shell_number(stack_str):
            return float(stack_str)

        # If its not a number it is handled as a shell var
        var = stack_str
        if var.startswith("$"):
            var = var[1:]
            if var.startswith("{") and var.endswith("}"):
                var = var[1:-1]

        # Unset vars and vars set to empty strings are treated as 0
        value = _get_env_var_value(var, venv, default="0")
        if is_valid_shell_number(value):
            return float(value)
        else:
            raise ValueError("Invalid arithmetic expansion")

    # Main function body
    value = 0
    stack = []
    char = ""

    if isinstance(expression, str):
        # Whitespace is ignored when evaluating the expression
        expression = expression.replace(' ', "").replace(
            "\t", "").replace("\n", "")

        # Raise an error if the last char in the expression is an operator
        last_char = expression[-1] if expression else ""
        if last_char and (is_operator(last_char) or is_assignment_operator(last_char)):
            raise ValueError(
                f"Invalid arithmetic expansion. operand expected (error token is '{last_char}')")

        if expression.startswith("-"):
            operator = "-"
            # More than one leading - is allowed by shell but has no effect different from one -
            expression = expression.lstrip("-")
        else:
            # leading +(s) are allowed by shell but have no effect
            expression = expression.lstrip("+")

        # Create an iterator of all non-whitespace chars in the expression
        expr_iter = iter(expression)
    else:
        # If the expression is already an iterator (when called recursively) use it as is
        expr_iter = expression

    # Recursively evaluate the expression until the iterator is exhausted
    while (char := next(expr_iter, "")):
        did_lookahead = False

        if is_operator(char):
            # Check if the operator is followed by an equals sign "=" (+= or -=)
            next_char = next(expr_iter, "")
            did_lookahead = True

            # Evaluate the stack and update the value whenever a + or - is encountered,
            stack_value = evaluate_stack(stack, venv)
            if operator == "-":
                stack_value = -stack_value
            value += stack_value

            # Reset the stack to only next_char if the operator is not followed by an equals sign "="
            if not is_assignment_operator(next_char):
                stack = [next_char]

            # So assignment is handled correctly by the next if block
            operator = char
            char = next_char

        if is_assignment_operator(char):
            var = ''.join(stack)
            if not var:
                raise ValueError(
                    "Invalid arithmetic expansion. variable expected")

            # Recursively evaluate the expression after the assignment operator
            assignment_value = _simple_shell_math(expr_iter, venv, operator)
            if operator == "-":
                assignment_value = -assignment_value
            value += assignment_value

            # Set the variable to the evaluated value depending on whether it was an assignment or an increment
            if did_lookahead:
                # Increment the variable by the assignment value
                venv[var] = str(
                    int(float(venv.get(var, 0)) + assignment_value))
            else:
                # Set the variable to the assignment value
                venv[var] = str(assignment_value)

            # Clear the stack and continue to the next char
            stack.clear()

        elif not did_lookahead:
            # Add the char to the stack if not added during the lookahead
            stack.append(char)

    # Evaluate what is left in the stack after the iterator is exhausted
    stack_value = evaluate_stack(stack, venv)
    if operator == "-":
        stack_value = -stack_value
    value += stack_value

    # Floats can be used in shells but the value is truncated to an int
    return int(value)


def _shell_expand(command: str, 
                  venv: Optional[dict] = None,
                  BANNED_SHELL_EXPANSION_OPERATORS: ValidConfigVal = DEFAULT_BANNED_SHELL_EXPANSION_OPERATORS,
                  ALLOWED_SHELL_EXPANSION_OPERATORS: ValidConfigVal = DEFAULT_ALLOWED_SHELL_EXPANSION_OPERATORS
                  ) -> str:
    """
    Expand shell variables and shell expansions in the command string.
    Implementation is based on Bash expansion rules: 
    https://www.gnu.org/software/bash/manual/html_node/Shell-Expansions.html
    """

    PARAM_EXPANSION_REGEX = re_compile(
        r'(?P<fullexp>\$(?P<content>[a-zA-Z_][a-zA-Z0-9_]*|\{[^{}\$]+?\}))')
    BRACE_EXPANSION_REGEX = re_compile(
        r'(?P<fullexp>\S*(?P<content>\{[^{}\$]+?\})\S*)')

    # To store {placeholder : invalid_match} pairs to reinsert after the loop
    invalid_matches = {}
    venv = venv or {}  # To store env vars set during expansion
    if "IFS" not in venv:
        # Set the default IFS to space if it is not set explicitly in the environment
        # since it is not always returned correctly by os.getenv or os.path.expandvars on all systems
        venv["IFS"] = _get_env_var_value("IFS", venv, default=" ")

    while (match := (PARAM_EXPANSION_REGEX.search(command) or BRACE_EXPANSION_REGEX.search(command))):
        full_expansion, content = match.groups()
        inside_braces = content[1:-1] if content.startswith(
            "{") and content.endswith("}") else content

        if match.re is PARAM_EXPANSION_REGEX:
            # Handles Parameter expansion ${var:1:2}, ${var:1}, ${var:1:}, ${var:1:2:3}
            # and ${var:-defaultval}, ${var:=defaultval}, ${var:+defaultval}, ${var:?defaultval}
            # https://www.gnu.org/software/bash/manual/html_node/Shell-Parameter-Expansion.html

            # Blocks ${!prefix*} ${!prefix@} ${!name[@]} ${!name[*]} ${#parameter} ${parameter#word} ${parameter##word}
            # ${parameter/pattern/string} ${parameter%word} ${parameter%%word} ${parameter@operator}
            for banned_expansion_operator in BANNED_SHELL_EXPANSION_OPERATORS:
                if banned_expansion_operator in inside_braces:
                    raise SecurityException(
                        f"Disallowed shell expansion operator: {banned_expansion_operator}")

            var, *expansion_params = inside_braces.split(":")

            value, operator, default = "", "", ""
            start_slice, end_slice = None, None
            if expansion_params:
                expansion_param_1 = expansion_params[0]

                # If the first char is empty or a digit or a space then it is a slice expansion
                # like ${var:1:2}, ${var:1}, ${var:1:}, ${var:1:2:3} ${var: -1} ${var:1+1:5-2} ${var::}
                if not expansion_param_1 or expansion_param_1[0].isalnum() or expansion_param_1[0] == " ":
                    try:
                        start_slice = _simple_shell_math(
                            expansion_param_1, venv)
                        if len(expansion_params) > 1:
                            expansion_param_2 = expansion_params[1]
                            end_slice = _simple_shell_math(
                                expansion_param_2, venv)
                    except ValueError as e:
                        raise SecurityException(
                            f"Invalid arithmetic in shell expansion: {e}")

                elif (operator := expansion_param_1[0]) in ALLOWED_SHELL_EXPANSION_OPERATORS:
                    # If the first char is a shell expansion operator then it is a default value expansion
                    # like ${var:-defaultval}, ${var:=defaultval}, ${var:+defaultval}, ${var:?defaultval}
                    default = ':'.join(expansion_params)[1:]

            value = _get_env_var_value(var, venv, default="")
            if start_slice is not None:
                value = value[start_slice:end_slice]
            elif not operator or operator == "?":
                value = value
            elif operator in "-=":
                value = value or default
                if operator == "=":
                    # Store the value in the venv if the operator is =
                    venv[var] = value
            elif operator == "+":
                value = default if value else ""

            command = command.replace(full_expansion, value, 1)

        elif match.re is BRACE_EXPANSION_REGEX:
            # Handles Brace and sequence expansion like {1..10..2}, {a,b,c}, {1..10}, {1..-1}
            # https://www.gnu.org/software/bash/manual/html_node/Brace-Expansion.html
            values = []
            escape_placeholders = {
                f"{hash(full_expansion)}comma": "\\,",
                f"{hash(full_expansion)}lbrace": "\\{",
                f"{hash(full_expansion)}rbrace": "\\}",
            }
            # Docs state: "A { or ‘,’ may be quoted with a backslash to prevent its being considered part of a brace expression."
            inside_braces_no_escapes = _replace_all(
                inside_braces, escape_placeholders, reverse=True)

            if ',' in inside_braces_no_escapes and inside_braces_no_escapes.count("{") == inside_braces_no_escapes.count("}"):
                # Brace expansion
                for var in inside_braces_no_escapes.split(','):
                    var = _replace_all(var, escape_placeholders)
                    item = full_expansion.replace(
                        content, _strip_quotes(var), 1)
                    values.append(item)

            elif len(seq_params := inside_braces.split('..')) in (2, 3):
                # Sequence expansion
                start, end = seq_params[:2]

                if start.replace("-", "", 1).isdigit() and end.replace("-", "", 1).isdigit():
                    # Numeric sequences
                    start, end = int(start), int(end)
                    step = int(seq_params[2]) if len(seq_params) == 3 else 1
                    format_fn = str
                    valid_sequence = True
                elif start.isalnum() and end.isalnum() and len(start) == len(end) == 1:
                    # Alphanumeric sequences
                    start, end = ord(start), ord(end)
                    step = 1
                    format_fn = chr
                    # Step is not allowed for character sequences
                    valid_sequence = (len(seq_params) == 2)
                else:
                    # Invalid sequences
                    start, end, step = 0, 0, 0
                    valid_sequence = False

                if valid_sequence:
                    if start <= end and step > 0:
                        sequence = range(start, end+1, step)
                    elif start <= end and step < 0:
                        sequence = range(end-1, start-1, step)
                    elif start > end and step > 0:
                        sequence = range(start, end-1, -step)
                    elif start > end and step < 0:
                        sequence = reversed(range(start, end-1, step))
                    else:
                        # When syntax is valid but step is 0 the sequence is just the value inside the braces so the expansion is replaced with the value
                        sequence = [inside_braces]

                    # Apply the format function (str or chr) to each int in the sequence
                    values.extend(full_expansion.replace(
                        content, format_fn(i), 1) for i in sequence)

                else:
                    # Replace invalid expansion to prevent infinite loop (from matching again) and store the content to reinsert after the loop
                    placeholder = str(hash(content))
                    invalid_matches[placeholder] = content
                    values.append(full_expansion.replace(content, placeholder))

            # Replace the full expansion with the expanded values
            value = ' '.join(values)
            command = command.replace(full_expansion, value, 1)

    # Reinsert invalid matches after the loop exits
    command = _replace_all(command, invalid_matches)
    return command


def _space_redirection_operators(command: str) -> str:
    """
    Space out redirection operators to avoid them being combined with the next or previous command part when splitting.
    Implementation is based on Bash redirection rules:
    https://www.gnu.org/software/bash/manual/html_node/Redirections.html
    """
    REDIRECTION_OPERATORS_REGEX = re_compile(
        r'(?![<>]+\()(<<?<?[-&]?[-&p]?|(?:\d+|&)?>>?&?-?(?:\d+|\|)?|<>)')
    return REDIRECTION_OPERATORS_REGEX.sub(r' \1 ', command)


def _recursive_shlex_split(command: str) -> Iterator[str]:
    """
    Recursively split the command string using shlex.split to handle nested/quoted shell syntax.
    """
    for cmd_part in shlex.split(command, comments=True):
        yield cmd_part

        # Strip either type of quotes but not both
        cmd_part = _strip_quotes(cmd_part)

        if '"' in cmd_part or "'" in cmd_part or " " in cmd_part:
            yield from _recursive_shlex_split(cmd_part)


def _parse_command(command: ValidCommand, 
                   venv: Optional[dict] = None, 
                   shell: Optional[bool] = True, 
                   BANNED_SHELL_EXPANSION_OPERATORS: ValidConfigVal = DEFAULT_BANNED_SHELL_EXPANSION_OPERATORS,
                   ALLOWED_SHELL_EXPANSION_OPERATORS: ValidConfigVal = DEFAULT_ALLOWED_SHELL_EXPANSION_OPERATORS
                   ) -> Tuple[str, List[str]]:
    """
    Expands the shell exspansions in the command then parses the expanded command into a list of command parts.
    """
    if isinstance(command, str):
        command_str = command
    elif isinstance(command, list):
        command_str = " ".join(command)
    else:
        raise TypeError("Command must be a str or a list")

    if not command_str:
        # No need to expand or parse an empty command
        return ("", [])

    spaced_command = _space_redirection_operators(command_str)
    if shell:
        expanded_command = _shell_expand(
            spaced_command, venv, BANNED_SHELL_EXPANSION_OPERATORS, ALLOWED_SHELL_EXPANSION_OPERATORS)
    else:
        expanded_command = spaced_command
    parsed_command = list(_recursive_shlex_split(expanded_command))
    return expanded_command, parsed_command


def _path_is_executable(path: Path) -> bool:
    return access(path, X_OK)


def _resolve_executable_path(executable: Optional[str], venv: Optional[dict] = None) -> Optional[Path]:
    """
    Try to resolve the path of the executable using the which command and the system PATH.
    """
    if not executable:
        return None  # Return None if the executable is not set so does not resolve to /usr/local/bin

    if executable_path := which(executable, path=venv.get("PATH") if venv is not None else None):
        return Path(executable_path).resolve()

    # Explicitly check if the executable is in the system PATH or absolute when which fails
    for path in [""] + get_exec_path(env=venv if venv is not None else None):
        if (executable_path := Path(path) / executable).exists() and _path_is_executable(executable_path):
            return executable_path.resolve()

    return None


def _resolve_paths_in_parsed_command(parsed_command: List[str], venv: Optional[dict] = None) -> Tuple[Set[Path], Set[str]]:
    """
    Create Path objects from the parsed commands and resolve symlinks then add to sets of unique Paths 
    and absolute path strings for comparison with the sensitive files, common exploit executables and group/owner checks.
    """

    abs_paths, abs_path_strings = set(), set()

    for cmd_part in parsed_command:

        if "~" in cmd_part:
            # Expand ~ and ~user constructions in the cmd_part
            cmd_part = expanduser(cmd_part)

        # Check if the cmd_part is an executable and resolve the path
        if executable_path := _resolve_executable_path(cmd_part, venv):
            abs_paths.add(executable_path)
            abs_path_strings.add(str(executable_path))

        # Handle any globbing characters and repeating slashes from the command and resolve symlinks to get absolute path
        for path in iglob(cmd_part, recursive=True):
            path = Path(path)

            # When its a symlink both the absolute path of the symlink
            # and the resolved path of its target are added to the sets
            if path.is_symlink():
                path = path.absolute()
                abs_paths.add(path)
                abs_path_strings.add(str(path))

            abs_path = Path(path).resolve()
            abs_paths.add(abs_path)
            abs_path_strings.add(str(abs_path))

            # Check if globbing and/or resolving symlinks returned an executable and add to the sets
            if executable_path := _resolve_executable_path(str(path), venv):
                abs_paths.add(executable_path)
                abs_path_strings.add(str(executable_path))

            # Check if globbing and/or resolving symlinks returned a directory and add all files in the directory to the sets
            if abs_path.is_dir():
                for file in abs_path.rglob("*"):
                    file = file.resolve()
                    abs_paths.add(file)
                    abs_path_strings.add(str(file))

    return abs_paths, abs_path_strings

 # Restriction checks
def check(command: ValidCommand, config: Dict[str, ValidConfigVal], Popen_kwargs: dict, force_shell: bool = False) -> None:
    if not (RESTRICTIONS := config["RESTRICTIONS"]):
        # No restrictions no checks
        return None

    # venv is a copy to avoid modifying the original Popen kwargs or None to default to using os.environ when env is not set
    venv = dict(**Popen_env) if (Popen_env := Popen_kwargs.get("env")) is not None else None

    # Check if the executable is set by the Popen kwargs (either executable or shell)
    # Executable takes precedence over shell. see subprocess.py line 1593.
    # force_shell takes precedence over both and is used for subprocess.getstatusoutput and subprocess.getoutput 
    # which both always run with shell=True but do not accept a shell kwarg
    executable_path = _resolve_executable_path(Popen_kwargs.get("executable"), venv)
    shell = force_shell or (executable_path.name in config["SHELLS"] if executable_path else Popen_kwargs.get("shell"))

    expanded_command, parsed_command = _parse_command(command, venv, shell)
    if not parsed_command:
        # Empty commands are safe
        return None

    # If the executable is not set by the Popen kwargs it is the first command part (args). see subprocess.py line 1596
    if not executable_path:
        executable_path = _resolve_executable_path(parsed_command[0], venv)

    abs_paths, abs_path_strings = _resolve_paths_in_parsed_command(
        parsed_command, venv)

    if "PREVENT_COMMAND_CHAINING" in RESTRICTIONS:
        check_multiple_commands(
            expanded_command,
            parsed_command,
            config["BANNED_COMMAND_CHAINING_SEPARATORS"],
            config["BANNED_COMMAND_AND_PROCESS_SUBSTITUTION_OPERATORS"],
            config["BANNED_COMMAND_CHAINING_EXECUTABLES"],
            config["SHELLS"]
        )

    if "PREVENT_ARGUMENTS_TARGETING_SENSITIVE_FILES" in RESTRICTIONS:
        check_sensitive_files(
            expanded_command,
            abs_path_strings,
            config["SENSITIVE_FILE_PATHS"]
        )

    if "PREVENT_COMMON_EXPLOIT_EXECUTABLES" in RESTRICTIONS:
        check_banned_common_exploit_executables(
            expanded_command, 
            abs_path_strings,
            config["BANNED_COMMON_EXPLOIT_EXECUTABLES"]    
        )

    PREVENT_UNCOMMON_PATH_TYPES = "PREVENT_UNCOMMON_PATH_TYPES" in RESTRICTIONS
    PREVENT_ADMIN_OWNED_FILES = "PREVENT_ADMIN_OWNED_FILES" in RESTRICTIONS
    
    # Only extract vals from config and loop through paths when checks are needed
    if (PREVENT_UNCOMMON_PATH_TYPES or PREVENT_ADMIN_OWNED_FILES):   
        BANNED_PATHTYPES = config["BANNED_PATHTYPES"]
        BANNED_OWNERS = config["BANNED_OWNERS"]
        BANNED_GROUPS = config["BANNED_GROUPS"]

        for path in abs_paths:
            # to avoid blocking the executable itself since most are symlinks to the actual executable
            # and owned by root with group wheel or sudo
            if path == executable_path:
                continue

            if PREVENT_UNCOMMON_PATH_TYPES:
                check_path_type(path, BANNED_PATHTYPES)

            if PREVENT_ADMIN_OWNED_FILES:
                check_file_owner(path, BANNED_OWNERS)
                check_file_group(path, BANNED_GROUPS)


def check_multiple_commands(expanded_command: str,
                            parsed_command: List[str],
                            BANNED_COMMAND_CHAINING_SEPARATORS: ValidConfigVal = DEFAULT_BANNED_COMMAND_CHAINING_SEPARATORS,
                            BANNED_COMMAND_AND_PROCESS_SUBSTITUTION_OPERATORS: ValidConfigVal = DEFAULT_BANNED_COMMAND_AND_PROCESS_SUBSTITUTION_OPERATORS,
                            BANNED_COMMAND_CHAINING_EXECUTABLES: ValidConfigVal = DEFAULT_BANNED_COMMAND_CHAINING_EXECUTABLES,
                            SHELLS: ValidConfigVal = DEFAULT_SHELLS
                            ) -> None:
    # Since shlex.split removes newlines from the command, it would not be present in the parsed_command and
    # must be checked for in the expanded command string
    if '\n' in expanded_command:
        raise SecurityException(
            "Multiple commands not allowed. Newline found.")

    for cmd_part in parsed_command:
        if any(seperator in cmd_part for seperator in BANNED_COMMAND_CHAINING_SEPARATORS):
            raise SecurityException(
                f"Multiple commands not allowed. Separators found.")

        if any(substitution_op in cmd_part for substitution_op in BANNED_COMMAND_AND_PROCESS_SUBSTITUTION_OPERATORS):
            raise SecurityException(
                f"Multiple commands not allowed. Process substitution operators found.")

        if cmd_part.strip() in BANNED_COMMAND_CHAINING_EXECUTABLES | SHELLS:
            raise SecurityException(
                f"Multiple commands not allowed. Executable {cmd_part} allows command chaining.")


def check_sensitive_files(expanded_command: str,
                          abs_path_strings: Set[str],
                          SENSITIVE_FILE_PATHS: ValidConfigVal = DEFAULT_SENSITIVE_FILE_PATHS
                          ) -> None:
    for sensitive_path in SENSITIVE_FILE_PATHS:
        # First check the absolute path strings for the sensitive files
        # Then handle edge cases when a sensitive file is part of a command but the path could not be resolved
        if (
            any(abs_path_string.endswith(sensitive_path)
                for abs_path_string in abs_path_strings)
            or sensitive_path in expanded_command
        ):
            raise SecurityException(
                f"Disallowed access to sensitive file: {sensitive_path}")


def check_banned_common_exploit_executables(expanded_command: str,
                            abs_path_strings: Set[str],
                            BANNED_COMMON_EXPLOIT_EXECUTABLES: ValidConfigVal = DEFAULT_BANNED_COMMON_EXPLOIT_EXECUTABLES
                            ) -> None:
    for banned_executable in BANNED_COMMON_EXPLOIT_EXECUTABLES:
        # First check the absolute path strings for the banned executables
        # Then handle edge cases when a banned executable is part of a command but the path could not be resolved
        if (
            any((abs_path_string.endswith(
                f"/{banned_executable}") for abs_path_string in abs_path_strings))
            or expanded_command.startswith(f"{banned_executable} ")
            or f"bin/{banned_executable}" in expanded_command
            or f" {banned_executable} " in expanded_command
        ):
            raise SecurityException(
                f"Disallowed command: {banned_executable}")


def check_path_type(path: Path,
                    BANNED_PATHTYPES: ValidConfigVal = DEFAULT_BANNED_PATHTYPES
                    ) -> None:
    for pathtype in BANNED_PATHTYPES:
        if getattr(path, f"is_{pathtype}")():
            raise SecurityException(
                f"Disallowed access to path type {pathtype}: {path}")


def check_file_owner(path: Path,
                     BANNED_OWNERS: ValidConfigVal = DEFAULT_BANNED_OWNERS
                     ) -> None:
    owner = path.owner()
    if owner in BANNED_OWNERS:
        raise SecurityException(
            f"Disallowed access to file owned by {owner}: {path}")


def check_file_group(path: Path,
                     BANNED_GROUPS: ValidConfigVal = DEFAULT_BANNED_GROUPS
                     ) -> None:
    group = path.group()
    if group in BANNED_GROUPS:
        raise SecurityException(
            f"Disallowed access to file owned by {group}: {path}")



if __name__ == "__main__":
    ### Example usage with module level functions
    run("echo hello", shell=True)
    call("/bin/ls") # Both allowed since commands do not trigger any restrictions

    try:
        # Blocked by the PREVENT_ARGUMENTS_TARGETING_SENSITIVE_FILES restriction
        run("cat /etc/passwd", shell=True, restrictions={"PREVENT_ARGUMENTS_TARGETING_SENSITIVE_FILES"})
    except SecurityException as e:
        print(e)

    try:
        # Blocked since /secret/file is a sensitive file
        run("cat /secret/file", shell=True,
            restrictions={"PREVENT_ARGUMENTS_TARGETING_SENSITIVE_FILES"},
            sensitive_file_paths={"/secret/file"}
        )
    except SecurityException as e:
        print(e)       


    ### Example usage with SafeCommandRunner/SafeCommand class
    sc = SafeCommandRunner(
        restrictions={"PREVENT_COMMON_EXPLOIT_EXECUTABLES", "PREVENT_ARGUMENTS_TARGETING_SENSITIVE_FILES"}, 
        allow_common_exploit_executables={"nc"}
    )
    print(sc)
    sc.run("nc -h", shell=True) # Allowed since nc is explicitly allowed in constructor
    
    sc.update_config(add_banned_common_exploit_executables={"nc"}) # Add nc to the banned executables
    print(sc)
    try:
        sc.run("nc -h", shell=True) # Now blocked after updating the config
    except SecurityException as e:
        print(e)

    try:
        sc.run("cat /etc/passwd", shell=True) # Blocked by the PREVENT_ARGUMENTS_TARGETING_SENSITIVE_FILES restriction
    except SecurityException as e:
        print(e)

    sc.update_restrictions(None) # Remove all restrictions. Note this is same as sc.update_config(restrictions=None)
    print(sc)
    sc.run("cat /etc/passwd", shell=True) # No restrictions so now allowed