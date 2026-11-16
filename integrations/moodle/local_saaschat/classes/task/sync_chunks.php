<?php
namespace local_saaschat\task;

defined('MOODLE_INTERNAL') || die();

/**
 * Hourly: keep local_saaschat_chunks in step with the workspace's documents in saaschat.
 * Only runs when "saaschat + copy in Moodle" is chosen for the workspace in saaschat.
 */
class sync_chunks extends \core\task\scheduled_task {
    public function get_name() {
        return get_string('task_sync_chunks', 'local_saaschat');
    }

    public function execute() {
        global $CFG, $DB;
        require_once($CFG->libdir . '/filelib.php');

        $server = rtrim((string) get_config('local_saaschat', 'serverurl'), '/');
        $key = (string) get_config('local_saaschat', 'widgetkey');
        $code = (string) get_config('local_saaschat', 'sharedcode');
        if ($server === '' || $key === '' || $code === '') {
            mtrace('saaschat: not configured, nothing to copy.');
            return;
        }
        $base = $server . '/w/' . rawurlencode($key) . '/moodle/documents/';

        [$status, $data] = $this->get($base, $code);
        if ($status === 409) {
            mtrace('saaschat: copying passages to Moodle is turned off for this workspace.');
            return;
        }
        if ($status !== 200 || !isset($data['documents'])) {
            throw new \moodle_exception('syncfailed', 'local_saaschat', '', $status);
        }

        $local = $DB->get_records_sql_menu('SELECT DISTINCT docid, docversion FROM {local_saaschat_chunks}');
        $seen = [];
        foreach ($data['documents'] as $doc) {
            $id = (int) $doc['id'];
            $seen[$id] = true;
            if (isset($local[$id]) && $local[$id] === $doc['version']) {
                continue; // Unchanged.
            }
            [$status, $chunks] = $this->get($base . $id . '/chunks/', $code);
            if ($status === 404) {
                continue; // Deleted since the listing; removed next run.
            }
            if ($status !== 200 || !isset($chunks['chunks'])) {
                throw new \moodle_exception('syncfailed', 'local_saaschat', '', $status);
            }
            $now = time();
            $records = array_map(fn($c) => (object) [
                'docid' => $id,
                'docname' => \core_text::substr($doc['name'], 0, 255),
                'docversion' => $doc['version'],
                'chunkindex' => (int) $c['index'],
                'heading' => \core_text::substr((string) $c['section'], 0, 300),
                'content' => (string) $c['text'],
                'timemodified' => $now,
            ], $chunks['chunks']);
            $transaction = $DB->start_delegated_transaction();
            $DB->delete_records('local_saaschat_chunks', ['docid' => $id]);
            $DB->insert_records('local_saaschat_chunks', $records);
            $transaction->allow_commit();
            mtrace("saaschat: copied {$doc['name']} (" . count($records) . ' passages).');
        }
        foreach (array_keys($local) as $id) {
            if (!isset($seen[$id])) {
                $DB->delete_records('local_saaschat_chunks', ['docid' => $id]);
                mtrace("saaschat: removed document {$id}.");
            }
        }
    }

    /** GET with the shared code; returns [HTTP status, decoded JSON or null]. */
    private function get(string $url, string $code): array {
        $curl = new \curl();
        $curl->setHeader(['Authorization: Bearer ' . $code, 'Accept: application/json']);
        $body = $curl->get($url);
        $status = (int) ($curl->get_info()['http_code'] ?? 0);
        return [$status, json_decode((string) $body, true)];
    }
}
