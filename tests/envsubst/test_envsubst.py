import pytest
from docker_stack.envsubst import envsubst, SubstitutionError

def test_basic_substitution():
    env = {"MY_VAR": "hello"}
    template = "This is ${MY_VAR} world."
    assert envsubst(template, env=env) == "This is hello world."

def test_substitution_without_braces():
    env = {"MY_VAR": "hello"}
    template = "This is $MY_VAR world."
    assert envsubst(template, env=env) == "This is hello world."

def test_substitution_with_default_value():
    env = {}
    template = "This is ${MY_VAR:-default} world."
    assert envsubst(template, env=env) == "This is default world."

def test_substitution_with_default_value_and_env_set():
    env = {"MY_VAR": "actual"}
    template = "This is ${MY_VAR:-default} world."
    assert envsubst(template, env=env) == "This is actual world."

def test_multiple_substitutions():
    env = {"VAR1": "value1", "VAR2": "value2"}
    template = "${VAR1} and ${VAR2}."
    assert envsubst(template, env=env) == "value1 and value2."

def test_mixed_syntax_and_default():
    env = {"VAR_A": "A", "VAR_C": "C"}
    template = "$VAR_A and ${VAR_B:-B} and $VAR_C."
    assert envsubst(template, env=env) == "A and B and C."

def test_escaped_dollar_sign():
    env = {"VAR": "value"}
    template = "This is $$VAR and ${VAR}."
    assert envsubst(template, env=env) == "This is $$VAR and value."

def test_no_substitution():
    env = {}
    template = "This is a plain string."
    assert envsubst(template, env=env) == "This is a plain string."

def test_empty_string():
    env = {}
    template = ""
    assert envsubst(template, env=env) == ""

def test_variable_with_empty_value():
    env = {"EMPTY_VAR": ""}
    template = "Value: ${EMPTY_VAR}"
    assert envsubst(template, env=env) == "Value: "

def test_variable_with_empty_default():
    env = {}
    template = "Value: ${MY_VAR:-}"
    assert envsubst(template, env=env) == "Value: "

def test_variable_with_special_characters_in_value():
    env = {"SPECIAL_VAR": "value-with_!@#$"}
    template = "Special: ${SPECIAL_VAR}"
    assert envsubst(template, env=env) == "Special: value-with_!@#$"

def test_variable_with_numbers_and_underscores():
    env = {"VAR_123_ABC": "test"}
    template = "Test: $VAR_123_ABC"
    assert envsubst(template, env=env) == "Test: test"

def test_replacement_map_basic():
    env = {"MY_VAR": "value_with_$"}
    template = "Result: ${MY_VAR}"
    replacements = {"$": "$$"}
    assert envsubst(template, env=env, replacements=replacements) == "Result: value_with_$$"

def test_replacement_map_multiple_replacements():
    env = {"MY_VAR": "value_with_$_and_#"}
    template = "Result: ${MY_VAR}"
    replacements = {"$": "$$", "#": "##"}
    assert envsubst(template, env=env, replacements=replacements) == "Result: value_with_$$_and_##"

def test_replacement_map_no_match():
    env = {"MY_VAR": "value_without_special_chars"}
    template = "Result: ${MY_VAR}$$"
    replacements = {"$": "$$"}
    assert envsubst(template, env=env, replacements=replacements) == "Result: value_without_special_chars$$"

def test_replacement_map_empty():
    env = {"MY_VAR": "value_with_$"}
    template = "Result: ${MY_VAR}"
    replacements = {}
    assert envsubst(template, env=env, replacements=replacements) == "Result: value_with_$"

def test_replacement_map_with_default_value():
    env = {}
    template = "Result: ${MY_VAR:-default_with_$}"
    replacements = {"$": "$$"}
    assert envsubst(template, env=env, replacements=replacements) == "Result: default_with_$$"

def test_replacement_map_with_env_and_default():
    env = {"MY_VAR": "env_value_with_$"}
    template = "Result: ${MY_VAR:-default_with_$}"
    replacements = {"$": "$$"}
    assert envsubst(template, env=env, replacements=replacements) == "Result: env_value_with_$$"

def test_missing_variable_error_collection_throw():
    env = {}
    template = "Missing: $VAR1\nAlso missing: ${VAR2}\nAnother line: $VAR3"

    with pytest.raises(SubstitutionError) as excinfo:
        envsubst(template, env=env, on_error='throw')

    results = excinfo.value.results
    assert len(results) == 3

    # Check for VAR1 error
    var1_error = next((r for r in results if r.variable_name == 'VAR1'), None)
    assert var1_error is not None
    assert var1_error.line_no == 1
    assert var1_error.line_content == "Missing: $VAR1"

    # Check for VAR2 error
    var2_error = next((r for r in results if r.variable_name == 'VAR2'), None)
    assert var2_error is not None
    assert var2_error.line_no == 2
    assert var2_error.line_content == "Also missing: ${VAR2}"

    # Check for VAR3 error
    var3_error = next((r for r in results if r.variable_name == 'VAR3'), None)
    assert var3_error is not None
    assert var3_error.line_no == 3
    assert var3_error.line_content == "Another line: $VAR3"

def test_no_error_on_valid_substitution(capsys):
    env = {"VAR1": "val1", "VAR2": "val2"}
    template = "Valid: $VAR1\nAnother valid: ${VAR2}"
    
    result = envsubst(template, env=env)
    
    captured = capsys.readouterr()
    assert "ERROR" not in captured.err
    assert result == "Valid: val1\nAnother valid: val2"

def test_complex_template_with_replacements():
    env = {
        "APP_VERSION": "1.0.0$",
        "DB_PASSWORD": "my_secret_pa$$word"
    }
    template = """
    version: '3.8'
    services:
      app:
        image: myapp:${APP_VERSION}
        environment:
          DB_PASS: ${DB_PASSWORD}
          DB_PASS2: $DB_PASSWORD

          API_KEY: some_key_with_$$_sign
    """
    replacements = {"$": "$$"}
    expected_output = """
    version: '3.8'
    services:
      app:
        image: myapp:1.0.0$$
        environment:
          DB_PASS: my_secret_pa$$$$word
          DB_PASS2: my_secret_pa$$$$word

          API_KEY: some_key_with_$$_sign
    """
    assert envsubst(template, env=env, replacements=replacements).strip() == expected_output.strip()

def test_error_formatting_with_context():
    template = """
Line 1: No variables here.
Line 2: A valid variable ${VALID_VAR}.
Line 3: Missing one variable here -> $MISSING_1 <-
Line 4: Another line without variables.
Line 5: Two missing variables -> $MISSING_2 and $MISSING_3 <-
Line 6: Final line.
"""
    env = {"VALID_VAR": "value"}
    
    with pytest.raises(SubstitutionError) as excinfo:
        envsubst(template, env=env, on_error='throw')
    
    expected_error_message = """
  2   Line 1: No variables here.
  3   Line 2: A valid variable ${VALID_VAR}.
  4   Line 3: Missing one variable here -> $M\u0333I\u0333S\u0333S\u0333I\u0333N\u0333G\u0333_\u03331\u0333 <-
  5   Line 4: Another line without variables.
  6   Line 5: Two missing variables -> $M\u0333I\u0333S\u0333S\u0333I\u0333N\u0333G\u0333_\u03332\u0333 and $M\u0333I\u0333S\u0333S\u0333I\u0333N\u0333G\u0333_\u03333\u0333 <-
  7   Line 6: Final line.
"""
    # We need to compare them line by line, ignoring leading/trailing whitespace on each line
    actual_lines = str(excinfo.value).strip().split('\n')
    expected_lines = expected_error_message.strip().split('\n')
    
    assert len(actual_lines) == len(expected_lines)
    for actual, expected in zip(actual_lines, expected_lines):
        assert actual.strip() == expected.strip()
