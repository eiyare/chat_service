<?php
namespace local_saaschat\privacy;

defined('MOODLE_INTERNAL') || die();

use core_privacy\local\metadata\collection;
use core_privacy\local\request\approved_contextlist;
use core_privacy\local\request\contextlist;

/**
 * Stores no personal data in Moodle (the passage copy holds document text only). Signed-in
 * students' ID and name are sent to saaschat, which keeps their chat conversations. With
 * course content turned on, forum posts every student in a course can read (with their
 * authors' names) are sent too. Requests to export or delete data held in saaschat are
 * handled by the saaschat workspace owner.
 */
class provider implements \core_privacy\local\metadata\provider, \core_privacy\local\request\plugin\provider {
    public static function get_metadata(collection $collection): collection {
        $collection->add_external_location_link('saaschat', [
            'userid' => 'privacy:metadata:saaschat:userid',
            'fullname' => 'privacy:metadata:saaschat:fullname',
            'messages' => 'privacy:metadata:saaschat:messages',
            'courseid' => 'privacy:metadata:saaschat:courseid',
        ], 'privacy:metadata:saaschat');
        $collection->add_external_location_link('saaschat_forums', [
            'fullname' => 'privacy:metadata:saaschat_forums:fullname',
            'message' => 'privacy:metadata:saaschat_forums:message',
            'created' => 'privacy:metadata:saaschat_forums:created',
        ], 'privacy:metadata:saaschat_forums');
        return $collection;
    }

    public static function get_contexts_for_userid(int $userid): contextlist {
        return new contextlist();
    }

    public static function export_user_data(approved_contextlist $contextlist) {
    }

    public static function delete_data_for_all_users_in_context(\context $context) {
    }

    public static function delete_data_for_user(approved_contextlist $contextlist) {
    }
}
