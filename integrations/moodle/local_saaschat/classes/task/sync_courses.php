<?php
namespace local_saaschat\task;

defined('MOODLE_INTERNAL') || die();

use local_saaschat\client;
use local_saaschat\course_content;

/**
 * Hourly: send the chosen courses' content to saaschat, so students on a course's pages get
 * answers from that course. Only changed items are sent; removed ones are deleted in saaschat.
 * Needs "Course content" turned on for the workspace in saaschat.
 */
class sync_courses extends \core\task\scheduled_task {
    public function get_name() {
        return get_string('task_sync_courses', 'local_saaschat');
    }

    public function execute() {
        $client = client::configured();
        if (!$client) {
            mtrace('saaschat: not configured, nothing to send.');
            return;
        }
        [$status, $data] = $client->get('moodle/courses/');
        if ($status === 409) {
            mtrace('saaschat: course content is turned off for this workspace in saaschat.');
            return;
        }
        if ($status !== 200 || !isset($data['courses'])) {
            throw new \moodle_exception('coursesyncfailed', 'local_saaschat', '', $status);
        }
        $remote = array_map('intval', $data['courses']);
        $local = course_content::course_ids();

        foreach ($local as $courseid) {
            $this->sync_course($client, get_course($courseid));
        }
        foreach (array_diff($remote, $local) as $courseid) {
            [$status] = $client->post_json("moodle/courses/{$courseid}/delete/", []);
            $this->check($status, "removing course {$courseid}");
            mtrace("saaschat: removed course {$courseid}.");
        }
    }

    private function sync_course(client $client, \stdClass $course): void {
        [$status, $data] = $client->get("moodle/courses/{$course->id}/items/");
        $this->check($status, "listing course {$course->id}");
        $remote = (array) ($data['items'] ?? []);
        $content = new course_content($course, (array) ($data['extensions'] ?? ['md']), (int) ($data['max_file_bytes'] ?? 0));
        $items = $content->items();

        $dir = make_request_directory();
        $sent = 0;
        foreach ($items as $key => $item) {
            if (isset($remote[$key]) && $remote[$key] === $item['version']) {
                continue;  // unchanged
            }
            $fields = ['key' => $key, 'version' => $item['version'], 'name' => $item['name']];
            if (isset($item['file'])) {
                $fields['file'] = $item['file'];  // stored_file: Moodle's curl uploads it
            } else {
                $path = $dir . '/item.md';
                file_put_contents($path, $item['text']);
                $fields['file'] = new \CURLFile($path, 'text/markdown', clean_filename($key) . '.md');
            }
            [$status, $reply] = $client->post_form("moodle/courses/{$course->id}/items/", $fields);
            if ($status === 400 || $status === 413) {  // refused (type, size, empty): skip it
                mtrace("saaschat: course {$course->id}: skipped {$item['name']}: " . ($reply['error'] ?? $status));
                continue;
            }
            $this->check($status, "sending {$item['name']}", 202);
            $sent++;
        }
        $gone = array_values(array_diff(array_keys($remote), array_keys($items)));
        if ($gone) {
            [$status] = $client->post_json("moodle/courses/{$course->id}/items/delete/", ['keys' => $gone]);
            $this->check($status, "removing items from course {$course->id}");
        }
        mtrace("saaschat: course {$course->id} ({$course->shortname}): " . count($items) . " items, {$sent} sent, "
            . count($gone) . ' removed.');
    }

    private function check(int $status, string $what, int $expected = 200): void {
        if ($status !== $expected) {
            throw new \moodle_exception('coursesyncfailed', 'local_saaschat', '', "{$status} ({$what})");
        }
    }
}
