#!/usr/bin/python3
"""
NAME
       envsubst.py - substitutes environment variables in bash format strings

DESCRIPTION
    envsubst.py is an upgrade of the POSIX command `envsubst`

    supported syntax:
      normal       - ${VARIABLE1} or $VARIABLE1
      with default - ${VARIABLE1:-somevalue}
"""

from dataclasses import dataclass
import os
import re
import sys
from typing import Dict, List, Literal, Optional


@dataclass
class LineCheckResult:
    line_no: int
    line_content: str
    variable_name: Optional[str] = None  # None means no error
    start_index: Optional[int] = None # Start index of the variable in the line_content

    @property
    def has_error(self) -> bool:
        return self.variable_name is not None

    def __str__(self):
        if self.has_error:
            return (f"ERROR :: Missing variable: '{self.variable_name}' "
                    f"on line {self.line_no}: '{self.line_content}'")
        return f"OK    :: Line {self.line_no}: '{self.line_content}'"


class SubstitutionError(Exception):
    """Custom exception to collect multiple substitution errors with detailed information."""
    def __init__(self, results: List[LineCheckResult], template_str: str, underline_char: str = '\u0333'):
        self.results = results
        self.template_lines = template_str.splitlines(keepends=False)
        self.underline_char = underline_char
        super().__init__(self._format_messages(results))

    def _format_messages(self, results: List[LineCheckResult]) -> str:
        formatted_messages = []
        
        # Group results by line number
        errors_by_line = {}
        for result in results:
            if result.has_error:
                if result.line_no not in errors_by_line:
                    errors_by_line[result.line_no] = {
                        'line_content': result.line_content,
                        'variables': []
                    }
                errors_by_line[result.line_no]['variables'].append({
                    'name': result.variable_name,
                    'start_index': result.start_index
                })
        
        # Sort line numbers
        sorted_line_nos = sorted(errors_by_line.keys())
        
        last_printed_line = 0

        for line_no in sorted_line_nos:
            # Add separator if there's a gap from the last context block
            if last_printed_line > 0 and (line_no - 2) > (last_printed_line + 1):
                formatted_messages.append("")

            # Determine context lines to display
            start_context_line = max(last_printed_line + 1, line_no - 2)
            end_context_line = min(len(self.template_lines), line_no + 2)

            for current_ln in range(start_context_line, end_context_line + 1):
                line_text = self.template_lines[current_ln - 1]
                
                # If this is an error line, apply the underlining
                if current_ln in errors_by_line:
                    current_line_errors = errors_by_line[current_ln]['variables']
                    
                    # Sort errors by start_index in reverse to avoid index shifting issues
                    current_line_errors.sort(key=lambda x: x['start_index'], reverse=True)
                    
                    modified_line_text_chars = list(line_text) # Convert to list for mutability
                    
                    for var_info in current_line_errors:
                        var_name = var_info['name']
                        start_idx = var_info['start_index']
                        
                        # Insert underline_char after each character of the variable name
                        for k in range(len(var_name) - 1, -1, -1):
                            insert_pos = start_idx + k + 1 # Position after the character
                            modified_line_text_chars.insert(insert_pos, self.underline_char)
                    
                    line_text = "".join(modified_line_text_chars)

                formatted_messages.append(f"{current_ln:3d}   {line_text}")
            
            last_printed_line = end_context_line

        return "\n".join(formatted_messages)



def envsubst(template_str, env=os.environ, replacements: Dict[str, str] = None, on_error: Literal['exit','throw'] = 'exit'):
    """Substitute environment variables in the template string, supporting default values."""

    # Combined regex for ${VAR:-default} and $VAR, and also $$
    pattern = re.compile(r"\$\_ESCAPED_DOLLAR_|\$\{([^}:\s]+)(?::-(.*?))?\}|\$([a-zA-Z_][a-zA-Z0-9_]*)")

    # Handle escaped dollars
    template_str = template_str.replace("$$", "$_ESCAPED_DOLLAR_")

    lines = template_str.splitlines(True) # keepends=True
    processed_lines = []
    error_results: List[LineCheckResult] = []

    for i, original_line in enumerate(lines):
        line_no = i + 1
        
        line_errors_raw = [] # Store (var_name, start_index) tuples

        def replacer(match: re.Match[str]):
            if match.group(0) == '$_ESCAPED_DOLLAR_':
                return '$$'
            # Group 1, 2 for ${VAR:-default}
            if match.group(1) is not None:
                var = match.group(1)
                default_value = match.group(2) if match.group(2) is not None else None
                result = env.get(var, default_value)
                if result is None:
                    line_errors_raw.append((var, match.start(1))) # Use match.start(1) for ${VAR}
                    return match.group(0) # Keep original if variable not found
            # Group 3 for $VAR
            else:
                var = match.group(3)
                result = env.get(var, None)
                if result is None:
                    line_errors_raw.append((var, match.start(3))) # Use match.start(3) for $VAR
                    return match.group(0) # Keep original if variable not found
            
            if replacements:
                for old, new in replacements.items():
                    result = result.replace(old, new)
            return result

        processed_line = pattern.sub(replacer, original_line)
        
        processed_lines.append(processed_line)
        
        if line_errors_raw:
            # The original line content for error reporting should not have the escaped dollar placeholder.
            # It should also retain its leading/trailing whitespace for accurate caret positioning.
            error_line_content_for_report = original_line.replace("$_ESCAPED_DOLLAR_", "$$").rstrip('\n') # Remove only trailing newline
            
            # Use a set of tuples to store unique (var_name, start_index) pairs for this line
            unique_errors_on_line = set()
            for var_name, start_index in line_errors_raw:
                unique_errors_on_line.add((var_name, start_index))

            for var_name, start_index in sorted(list(unique_errors_on_line), key=lambda x: x[1]): # Sort by start_index
                error_results.append(LineCheckResult(line_no=line_no, line_content=error_line_content_for_report, variable_name=var_name, start_index=start_index))

    if error_results:
        # Sort errors by line number, then by start_index
        error_results.sort(key=lambda x: (x.line_no, x.start_index))
        if on_error == 'exit':
            error_output = SubstitutionError(error_results, template_str)._format_messages(error_results) # Pass template_str
            print(error_output, file=sys.stderr)
            exit(1)
        elif on_error == 'throw':
            raise SubstitutionError(error_results, template_str) # Pass template_str

    result_str = "".join(processed_lines)
    # Restore escaped dollars
    result_str = result_str.replace("$_ESCAPED_DOLLAR_", "$$")

    return result_str


def envsubst_load_file(template_file, env=os.environ, replacements: Dict[str, str] = None, on_error: str = 'exit'):
    with open(template_file) as file:
        return envsubst(file.read(), env, replacements, on_error)

def main():
    if len(sys.argv) > 2:
        print("Usage: python envsubst.py [template_file]")
        sys.exit(1)

    if len(sys.argv) == 2:
        template_file = sys.argv[1]
        with open(template_file, "r") as file:
            template_str = file.read()
    else:
        template_str = sys.stdin.read()

    result = envsubst(template_str)
    print(result)


if __name__ == "__main__":
    main()
