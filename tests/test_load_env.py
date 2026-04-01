import textwrap

import pytest

from docker_stack.cli import EnvFileResolutionError, load_env_file


def write_env(tmp_path, content: str):
    env_file = tmp_path / ".env"
    env_file.write_text(textwrap.dedent(content).lstrip())
    return env_file


def test_load_env_file_resolves_forward_reference_order_independent(tmp_path):
    env_file = write_env(
        tmp_path,
        """
        C=${A}_ing
        A=someth
        """,
    )

    values = load_env_file(str(env_file), base_env={})

    assert values["A"] == "someth"
    assert values["C"] == "someth_ing"


def test_load_env_file_strips_matching_quotes_from_values(tmp_path):
    env_file = write_env(
        tmp_path,
        """
        PINATA_API_JWT="abc.def"
        IPFS_PROJECT_ID=''
        """,
    )

    values = load_env_file(str(env_file), base_env={})

    assert values["PINATA_API_JWT"] == "abc.def"
    assert values["IPFS_PROJECT_ID"] == ""


def test_load_env_file_resolves_references_inside_quoted_values(tmp_path):
    env_file = write_env(
        tmp_path,
        """
        C="${A}_ing"
        A="someth"
        """,
    )

    values = load_env_file(str(env_file), base_env={})

    assert values["A"] == "someth"
    assert values["C"] == "someth_ing"


def test_load_env_file_resolves_chained_references_within_five_cycles(tmp_path):
    env_file = write_env(
        tmp_path,
        """
        E=${D}_e
        D=${C}_d
        C=${B}_c
        B=${A}_b
        A=base
        """,
    )

    values = load_env_file(str(env_file), base_env={})

    assert values["E"] == "base_b_c_d_e"


def test_load_env_file_resolves_long_acyclic_dependency_chain(tmp_path):
    chain = "\n".join([f"V{i}=${{V{i+1}}}" for i in range(10)] + ["V10=ok"])
    env_file = write_env(tmp_path, chain)

    values = load_env_file(str(env_file), base_env={})

    assert values["V0"] == "ok"
    assert values["V10"] == "ok"


def test_load_env_file_reports_missing_variable_with_line_location(tmp_path):
    env_file = write_env(
        tmp_path,
        """
        A=${MISSING}_suffix
        """,
    )

    with pytest.raises(EnvFileResolutionError) as excinfo:
        load_env_file(str(env_file), base_env={})

    message = str(excinfo.value)
    assert "Missing environment variables" in message
    assert str(env_file) in message
    assert "1   A=${M̳I̳S̳S̳I̳N̳G̳}_suffix" in message


def test_load_env_file_reports_missing_variable_inside_quoted_value_with_line_location(tmp_path):
    env_file = write_env(
        tmp_path,
        """
        A="${MISSING}_suffix"
        """,
    )

    with pytest.raises(EnvFileResolutionError) as excinfo:
        load_env_file(str(env_file), base_env={})

    message = str(excinfo.value)
    assert "Missing environment variables" in message
    assert str(env_file) in message
    assert '1   A="${M̳I̳S̳S̳I̳N̳G̳}_suffix"' in message


def test_load_env_file_reports_empty_variable_reference_with_line_location(tmp_path):
    env_file = write_env(
        tmp_path,
        """
        VAR1=
        IMAGE=myapp:${VAR1}
        """,
    )

    with pytest.raises(EnvFileResolutionError) as excinfo:
        load_env_file(str(env_file), base_env={})

    message = str(excinfo.value)
    assert "Missing environment variables" in message
    assert str(env_file) in message
    assert "2   IMAGE=myapp:${V̳A̳R̳1̳}" in message


def test_load_env_file_reports_cycle_with_line_locations(tmp_path):
    env_file = write_env(
        tmp_path,
        """
        A=${B}
        B=${A}
        """,
    )

    with pytest.raises(EnvFileResolutionError) as excinfo:
        load_env_file(str(env_file), base_env={})

    message = str(excinfo.value)
    assert "Cyclic environment variable references detected after 5 resolution passes" in message
    assert str(env_file) in message
    assert "1   A=${B̳}" in message
    assert "2   B=${A̳}" in message
