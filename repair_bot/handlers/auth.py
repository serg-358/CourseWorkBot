"""Собирает ролевые роутеры и сохраняет публичные функции сценариев."""

from aiogram import Router
from repair_bot.handlers import admin, client_requests, common, registration, staff_requests
from repair_bot.states import RegistrationStates, RequestStates

CLIENT_EDITABLE_STATUSES = common.CLIENT_EDITABLE_STATUSES
MAX_REQUEST_DESCRIPTION_LENGTH = common.MAX_REQUEST_DESCRIPTION_LENGTH
REQUESTS_PER_PAGE = common.REQUESTS_PER_PAGE
REQUEST_ADDITION_PREFIX = common.REQUEST_ADDITION_PREFIX
REQUEST_DESCRIPTION_PREVIEW_LENGTH = common.REQUEST_DESCRIPTION_PREVIEW_LENGTH
_demo_view_roles = common._demo_view_roles
_processed_start_messages = common._processed_start_messages
admin_request_text = common.admin_request_text
bounded_page = common.bounded_page
cancel_dialog = common.cancel_dialog
client_request_text = common.client_request_text
dashboard_text = common.dashboard_text
edit_callback_screen = common.edit_callback_screen
edit_state_screen = common.edit_state_screen
format_rubles = common.format_rubles
is_admin_telegram_id = common.is_admin_telegram_id
is_demo_switch_user = common.is_demo_switch_user
is_master_telegram_id = common.is_master_telegram_id
is_staff_telegram_id = common.is_staff_telegram_id
menu_for_user = common.menu_for_user
normalize_phone = common.normalize_phone
request_description_preview = common.request_description_preview
role_for_telegram_id = common.role_for_telegram_id
safe_edit_message = common.safe_edit_message
show_start_screen = common.show_start_screen
cancel_command = registration.cancel_command
cancel_button = registration.cancel_button
cmd_start = registration.cmd_start
switch_demo_view = registration.switch_demo_view
process_full_name = registration.process_full_name
process_non_text_name = registration.process_non_text_name
process_phone = registration.process_phone
process_non_text_phone = registration.process_non_text_phone
show_my_requests = client_requests.show_my_requests
show_page_info = client_requests.show_page_info
start_request_creation = client_requests.start_request_creation
start_request_addition = client_requests.start_request_addition
process_request_description = client_requests.process_request_description
process_non_text_description = client_requests.process_non_text_description
process_request_addition = client_requests.process_request_addition
process_non_text_addition = client_requests.process_non_text_addition
create_and_analyze_request = client_requests.create_and_analyze_request
finish_request_creation = client_requests.finish_request_creation
delete_ai_progress_message = client_requests.delete_ai_progress_message
skip_request_photo = client_requests.skip_request_photo
process_request_photo = client_requests.process_request_photo
process_invalid_request_photo = client_requests.process_invalid_request_photo
show_all_requests = staff_requests.show_all_requests
show_request_photo = staff_requests.show_request_photo
change_request_status = staff_requests.change_request_status
start_request_staff_edit = staff_requests.start_request_staff_edit
process_staff_diagnosis = staff_requests.process_staff_diagnosis
process_staff_price = staff_requests.process_staff_price
show_users = admin.show_users
show_administrators = admin.show_administrators
show_statistics = admin.show_statistics
show_profile = admin.show_profile

router = Router(name="auth")
router.include_routers(registration.router, client_requests.router, staff_requests.router, admin.router)

__all__ = [
    "RegistrationStates",
    "RequestStates",
    "CLIENT_EDITABLE_STATUSES",
    "MAX_REQUEST_DESCRIPTION_LENGTH",
    "REQUESTS_PER_PAGE",
    "REQUEST_ADDITION_PREFIX",
    "REQUEST_DESCRIPTION_PREVIEW_LENGTH",
    "_demo_view_roles",
    "_processed_start_messages",
    "admin_request_text",
    "bounded_page",
    "cancel_button",
    "cancel_command",
    "cancel_dialog",
    "change_request_status",
    "client_request_text",
    "cmd_start",
    "create_and_analyze_request",
    "dashboard_text",
    "delete_ai_progress_message",
    "edit_callback_screen",
    "edit_state_screen",
    "finish_request_creation",
    "format_rubles",
    "is_admin_telegram_id",
    "is_demo_switch_user",
    "is_master_telegram_id",
    "is_staff_telegram_id",
    "menu_for_user",
    "normalize_phone",
    "process_full_name",
    "process_invalid_request_photo",
    "process_non_text_addition",
    "process_non_text_description",
    "process_non_text_name",
    "process_non_text_phone",
    "process_phone",
    "process_request_addition",
    "process_request_description",
    "process_request_photo",
    "process_staff_diagnosis",
    "process_staff_price",
    "request_description_preview",
    "role_for_telegram_id",
    "router",
    "safe_edit_message",
    "show_administrators",
    "show_all_requests",
    "show_my_requests",
    "show_page_info",
    "show_profile",
    "show_request_photo",
    "show_start_screen",
    "show_statistics",
    "show_users",
    "skip_request_photo",
    "start_request_addition",
    "start_request_creation",
    "start_request_staff_edit",
    "switch_demo_view",
]
