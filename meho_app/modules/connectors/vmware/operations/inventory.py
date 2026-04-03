# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
VMware Operation Definitions - Split by Category

These are registered in the generic connector_operation table
so the agent can discover them via search_operations.
"""

from meho_app.modules.connectors.base import OperationDefinition

# INVENTORY OPERATIONS

INVENTORY_OPERATIONS = [
    OperationDefinition(
        operation_id="list_datacenters",
        name="List Datacenters",
        description="Get all datacenters in vCenter.",
        category="inventory",
        parameters=[],
        example="list_datacenters()",
        response_entity_type="Datacenter",
        response_identifier_field="moref_id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_folders",
        name="List Folders",
        description="Get all VM folders in vCenter.",
        category="inventory",
        parameters=[],
        example="list_folders()",
        response_entity_type="Folder",
        response_identifier_field="moref_id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="create_folder",
        name="Create Folder",
        description="Create a new folder in vCenter inventory. pyvmomi: CreateFolder(name)",
        category="inventory",
        parameters=[
            {
                "name": "parent_folder",
                "type": "string",
                "required": True,
                "description": "Parent folder name",
            },
            {
                "name": "folder_name",
                "type": "string",
                "required": True,
                "description": "Name for new folder",
            },
        ],
        example="create_folder(parent_folder='vm', folder_name='Production-VMs')",
    ),
    OperationDefinition(
        operation_id="rename_folder",
        name="Rename Folder",
        description="Rename a folder in vCenter inventory. pyvmomi: Rename_Task(newName)",
        category="inventory",
        parameters=[
            {
                "name": "folder_name",
                "type": "string",
                "required": True,
                "description": "Current folder name",
            },
            {
                "name": "new_name",
                "type": "string",
                "required": True,
                "description": "New folder name",
            },
        ],
        example="rename_folder(folder_name='old-folder', new_name='new-folder')",
    ),
    OperationDefinition(
        operation_id="destroy_folder",
        name="Delete Folder",
        description="Delete a folder. Folder must be empty. pyvmomi: Destroy_Task()",
        category="inventory",
        parameters=[
            {
                "name": "folder_name",
                "type": "string",
                "required": True,
                "description": "Name of folder to delete",
            },
        ],
        example="destroy_folder(folder_name='empty-folder')",
    ),
    OperationDefinition(
        operation_id="move_into_folder",
        name="Move Entity into Folder",
        description="Move a VM or folder into another folder. pyvmomi: MoveIntoFolder_Task(list)",
        category="inventory",
        parameters=[
            {
                "name": "target_folder",
                "type": "string",
                "required": True,
                "description": "Target folder name",
            },
            {
                "name": "entity_name",
                "type": "string",
                "required": True,
                "description": "Name of entity to move",
            },
            {
                "name": "entity_type",
                "type": "string",
                "required": True,
                "description": "Type: vm, folder",
            },
        ],
        example="move_into_folder(target_folder='Production-VMs', entity_name='web-01', entity_type='vm')",
    ),
    OperationDefinition(
        operation_id="register_vm",
        name="Register VM",
        description="Register an existing VM (vmx file) into vCenter inventory. pyvmomi: RegisterVM_Task(path, name, asTemplate, pool, host)",
        category="inventory",
        parameters=[
            {
                "name": "vmx_path",
                "type": "string",
                "required": True,
                "description": "Datastore path to VMX file",
            },
            {
                "name": "vm_name",
                "type": "string",
                "required": False,
                "description": "Name for the VM (default: from VMX)",
            },
            {
                "name": "folder_name",
                "type": "string",
                "required": False,
                "description": "Target folder",
            },
            {
                "name": "resource_pool",
                "type": "string",
                "required": False,
                "description": "Target resource pool",
            },
            {
                "name": "as_template",
                "type": "boolean",
                "required": False,
                "description": "Register as template (default: False)",
            },
        ],
        example="register_vm(vmx_path='[datastore1] recovered-vm/recovered-vm.vmx')",
    ),
    OperationDefinition(
        operation_id="list_content_libraries",
        name="List Content Libraries",
        description="List all content libraries in vCenter.",
        category="inventory",
        parameters=[],
        example="list_content_libraries()",
        response_entity_type="ContentLibrary",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_content_library_items",
        name="Get Content Library Items",
        description="List items in a content library.",
        category="inventory",
        parameters=[
            {
                "name": "library_name",
                "type": "string",
                "required": True,
                "description": "Name of content library",
            },
        ],
        example="get_content_library_items(library_name='Templates')",
        response_entity_type="ContentLibraryItem",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_tags",
        name="List Tags",
        description="List all tags defined in vCenter.",
        category="inventory",
        parameters=[],
        example="list_tags()",
        response_entity_type="Tag",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_tag_categories",
        name="List Tag Categories",
        description="List all tag categories in vCenter.",
        category="inventory",
        parameters=[],
        example="list_tag_categories()",
        response_entity_type="TagCategory",
        response_identifier_field="id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="list_templates",
        name="List VM Templates",
        description="List all VM templates in vCenter.",
        category="inventory",
        parameters=[],
        example="list_templates()",
        response_entity_type="Template",
        response_identifier_field="moref_id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_template",
        name="Get Template Details",
        description="Get details of a VM template.",
        category="inventory",
        parameters=[
            {
                "name": "template_name",
                "type": "string",
                "required": True,
                "description": "Name of the template",
            },
        ],
        example="get_template(template_name='ubuntu-template')",
        response_entity_type="Template",
        response_identifier_field="moref_id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="search_inventory",
        name="Search Inventory",
        description="Search vCenter inventory by name pattern.",
        category="inventory",
        parameters=[
            {
                "name": "name_pattern",
                "type": "string",
                "required": True,
                "description": "Name pattern to search (supports wildcards)",
            },
            {
                "name": "entity_type",
                "type": "string",
                "required": False,
                "description": "Entity type: vm, host, datastore, network, cluster",
            },
        ],
        example="search_inventory(name_pattern='web-*', entity_type='vm')",
        response_entity_type="InventoryEntity",
        response_identifier_field="moref_id",
        response_display_name_field="name",
    ),
    OperationDefinition(
        operation_id="get_inventory_path",
        name="Get Inventory Path",
        description="Get the full inventory path of an entity.",
        category="inventory",
        parameters=[
            {
                "name": "entity_name",
                "type": "string",
                "required": True,
                "description": "Name of entity",
            },
            {
                "name": "entity_type",
                "type": "string",
                "required": True,
                "description": "Entity type: vm, host, datastore, folder, cluster",
            },
        ],
        example="get_inventory_path(entity_name='web-01', entity_type='vm')",
    ),
]
