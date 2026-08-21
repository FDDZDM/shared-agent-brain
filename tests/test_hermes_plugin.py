from shared_brain.hermes_plugin import SharedBrainMemoryProvider


def test_hermes_provider_exposes_explicit_version_safe_tools():
    provider = SharedBrainMemoryProvider()
    schemas = {schema["name"]: schema for schema in provider.get_tool_schemas()}
    assert set(schemas) == {"brain_search", "brain_remember", "brain_update", "brain_forget"}
    assert "expected_version" in schemas["brain_update"]["parameters"]["required"]
    assert "expected_version" in schemas["brain_forget"]["parameters"]["required"]
    assert "untrusted reference data" in provider.system_prompt_block()

