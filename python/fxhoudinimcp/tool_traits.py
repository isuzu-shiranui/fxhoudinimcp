"""Which tools are read-only, and which tool groups a client is shown.

A tool is listed in READ_ONLY only when its handler itself changes nothing: the
scene and the current frame are left as found, and it writes no file. Probe
nodes created and destroyed inside a ``finally`` count as leaving the scene
alone. Reading geometry or a stage cooks the node, and a cook does whatever the
network does, a File SOP set to write included; that is the network's work, not
the tool's. Tools that cook at another frame without restoring it
(find_expensive_nodes, verify_network) or rewrite a node's code (validate_vex)
are not listed, and neither is anything new: an unlisted tool is reported as
possibly writing.

A tool's group is the module under ``fxhoudinimcp.tools`` that defines it.
"""

from __future__ import annotations

# Built-in
import os
import re
from pathlib import Path

READ_ONLY = frozenset(
    {
        # animation
        "get_frame",
        "get_keyframes",
        # cache
        "get_cache_status",
        "list_caches",
        # chops
        "get_chop_data",
        "list_chop_channels",
        # code
        "get_env_variable",
        "get_file_references",
        # context
        "compare_snapshots",
        "explain_node",
        "get_cook_chain",
        "get_network_overview",
        "get_node_errors_detailed",
        "get_scene_summary",
        "get_selection",
        # cops
        "get_cop_geometry",
        "get_cop_info",
        "get_cop_layer",
        "get_cop_vdb",
        "list_cop_node_types",
        # dops
        "get_dop_field",
        "get_dop_object",
        "get_dop_relationships",
        "get_sim_memory_usage",
        "get_simulation_info",
        "list_dop_objects",
        # geometry
        "compare_volumes",
        "find_nearest_point",
        "get_attrib_stats",
        "get_attrib_values",
        "get_attribute_info",
        "get_bounding_box",
        "get_geometry_info",
        "get_group_members",
        "get_groups",
        "get_points",
        "get_prim_intrinsics",
        "get_prims",
        "get_volume_info",
        "sample_geometry",
        "sample_volume",
        # graph
        "get_cook_status",
        "get_node_card",
        # hda
        "get_hda_info",
        "get_hda_section_content",
        "get_hda_sections",
        "list_hda_versions",
        "list_installed_hdas",
        # help
        "get_help_page",
        "get_workflow_guide",
        "search_help",
        # lops
        "find_usd_prims",
        "get_last_modified_prims",
        "get_stage_info",
        "get_usd_attribute",
        "get_usd_attributes",
        "get_usd_bound_material",
        "get_usd_composition",
        "get_usd_layers",
        "get_usd_materials",
        "get_usd_prim",
        "get_usd_prim_stats",
        "get_usd_variants",
        "get_usd_world_transform",
        "inspect_usd_layer",
        "list_lights",
        "list_usd_prims",
        # materials
        "get_material_info",
        "list_material_types",
        "list_materials",
        # nodes
        "find_nodes",
        "get_node_info",
        "list_children",
        "list_node_types",
        # parameters
        "get_expression",
        "get_parameter",
        "get_parameter_schema",
        "get_parameters",
        "get_parm_references",
        # rendering
        "get_render_progress",
        "get_render_settings",
        "list_render_nodes",
        # scene
        "get_context_info",
        "get_houdini_connection_status",
        "get_scene_info",
        # shelf
        "get_shelf_tool_script",
        "list_shelf_tools",
        # takes
        "get_current_take",
        "list_takes",
        # tops
        "get_failed_work_items",
        "get_pdg_graph",
        "get_top_logs",
        "get_top_network_info",
        "get_top_scheduler_info",
        "get_work_item_info",
        "get_work_item_states",
        # vex
        "get_wrangle_code",
        # viewport
        "find_error_nodes",
        "get_viewport_info",
        "list_panes",
    }
)

# Always registered: every tool the server instructions name lives in one of
# these, and the connection tools are needed to reach Houdini at all.
CORE_GROUPS = frozenset(
    {
        "cache",
        "code",
        "graph",
        "help",
        "nodes",
        "parameters",
        "rendering",
        "scene",
        "session",
        "viewport",
    }
)


def enabled_groups() -> frozenset[str] | None:
    """Groups to register besides CORE_GROUPS, or None for every group.

    FXHOUDINIMCP_TOOL_GROUPS is a comma-separated list such as ``lops,tops``.
    Unset, empty or ``all`` registers everything.
    """
    value = os.environ.get("FXHOUDINIMCP_TOOL_GROUPS", "").strip()
    if not value or value.lower() == "all":
        return None
    return frozenset(part.strip().lower() for part in value.split(",") if part.strip())


def all_groups() -> list[str]:
    tools_dir = Path(__file__).parent / "tools"
    return sorted(path.stem for path in tools_dir.glob("*.py") if path.stem != "__init__")


def groups_off() -> list[str]:
    groups = enabled_groups()
    if groups is None:
        return []
    return [g for g in all_groups() if g not in CORE_GROUPS and g not in groups]


def with_group_header(instructions: str) -> str:
    """*instructions* with the tool-count sentence saying some groups are off.

    The client shows only about 2,000 characters of instructions and the text
    already fills them, so the sentence is swapped for one of the same length
    rather than a note being added. get_houdini_connection_status names the
    groups that are off.
    """
    if not groups_off():
        return instructions
    return re.sub(
        r"(\d+) tools across \d+ categories",
        r"\1 tools, only some groups on",
        instructions,
        count=1,
    )


def group_of(function) -> str:
    return function.__module__.rsplit(".", 1)[-1]


def is_registered(function) -> bool:
    groups = enabled_groups()
    group = group_of(function)
    return groups is None or group in CORE_GROUPS or group in groups
