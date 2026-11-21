<?php
namespace local_saaschat;

defined('MOODLE_INTERNAL') || die();

/**
 * One signed-in student's own progress in a course, looked up for saaschat's assistant
 * while that student is asking (student.php).
 *
 * The caller must send the shared code and the student's own signed token (the one on the
 * page they are chatting from, see lib.php), so saaschat can only ever look up a student
 * who is signed in and talking to it. The lookup runs as that student, so Moodle's own
 * rules decide what they may see: hidden or not yet released grades stay hidden.
 */
class student_data {
    /** Tools saaschat may call. */
    const TOOLS = ['completion', 'grades', 'deadlines'];
    /** Characters of grade feedback kept per item. */
    const FEEDBACK_CHARS = 500;

    /** Answer the request as JSON. */
    public static function handle(): void {
        try {
            [$status, $data] = self::respond();
        } catch (\Throwable $e) {
            debugging('saaschat student lookup failed: ' . $e->getMessage(), DEBUG_DEVELOPER);
            [$status, $data] = [500, ['error' => 'The lookup failed in Moodle.']];
        }
        http_response_code($status);
        header('Content-Type: application/json; charset=utf-8');
        header('Cache-Control: no-store');
        echo json_encode($data);
    }

    private static function respond(): array {
        if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
            return [405, ['error' => 'POST only.']];
        }
        if (!get_config('local_saaschat', 'studentdata')) {
            return [403, ['error' => 'Looking up students\' own grades, completion and deadlines is turned off in the Moodle plugin settings.']];
        }
        $code = (string) get_config('local_saaschat', 'sharedcode');
        $sent = (string) ($_SERVER['HTTP_X_SAASCHAT_CODE'] ?? '');
        if ($code === '' || !hash_equals($code, $sent)) {
            return [403, ['error' => 'Wrong shared code.']];
        }
        $request = json_decode((string) file_get_contents('php://input'), true);
        if (!is_array($request)) {
            return [400, ['error' => 'Expected a JSON body.']];
        }
        $token = self::verify((string) ($request['token'] ?? ''), $code);
        if ($token === null || empty($token['course'])) {
            return [403, ['error' => 'The student\'s sign-in has expired. Reloading the page fixes this.']];
        }
        $tool = (string) ($request['tool'] ?? '');
        if (!in_array($tool, self::TOOLS, true)) {
            return [400, ['error' => 'Unknown tool.']];
        }

        $user = \core_user::get_user((int) $token['sub']);
        if (!$user || $user->deleted || $user->suspended) {
            return [403, ['error' => 'This user can\'t be looked up.']];
        }
        $course = get_course((int) $token['course']);
        \core\session\manager::set_user($user);  // from here on, Moodle's checks apply to the student
        if (!can_access_course($course, $user, '', true)) {
            return [403, ['error' => 'The student can\'t access this course.']];
        }
        switch ($tool) {
            case 'completion':
                $data = self::completion($course, $user);
                break;
            case 'grades':
                $data = self::grades($course, $user);
                break;
            default:
                $data = self::deadlines($course, $user);
        }
        return [200, ['tool' => $tool, 'course' => format_string($course->fullname), 'data' => $data]];
    }

    /** The token's data if signed with the shared code and unexpired (see lib.php). */
    private static function verify(string $token, string $code): ?array {
        if (substr_count($token, '.') !== 1) {
            return null;
        }
        [$payload, $signature] = explode('.', $token);
        if (!hash_equals(hash_hmac('sha256', $payload, $code), $signature)) {
            return null;
        }
        $data = json_decode(base64_decode(strtr($payload, '-_', '+/')), true);
        if (!is_array($data) || empty($data['sub']) || (int) ($data['exp'] ?? 0) < time()) {
            return null;
        }
        return $data;
    }

    /** Course completion criteria and each tracked activity's completion, for this student. */
    private static function completion(\stdClass $course, \stdClass $user): array {
        global $CFG;
        require_once($CFG->libdir . '/completionlib.php');
        $info = new \completion_info($course);
        if (!$info->is_enabled()) {
            return ['tracking' => false, 'note' => 'Completion tracking is not turned on for this course.'];
        }
        $out = ['tracking' => true];

        $completions = $info->get_completions($user->id);
        if ($completions) {
            $course_completion = new \completion_completion(['userid' => $user->id, 'course' => $course->id]);
            $out['course'] = [
                'complete' => $course_completion->is_complete(),
                'completed_on' => $course_completion->timecompleted ? userdate($course_completion->timecompleted) : null,
                'requires' => $info->get_aggregation_method() == COMPLETION_AGGREGATION_ANY
                    ? 'any one of the criteria' : 'all of the criteria',
                'criteria' => array_map(function($cc) {
                    $criterion = $cc->get_criteria();
                    return [
                        'criterion' => strip_tags($criterion->get_title_detailed()),
                        'type' => strip_tags($criterion->get_type_title()),
                        'status' => strip_tags($criterion->get_status($cc)),
                        'complete' => $cc->is_complete(),
                    ];
                }, array_values($completions)),
            ];
        } else {
            $out['course'] = ['note' => 'No course completion criteria are set; activity completion is listed below.'];
        }

        $states = [COMPLETION_INCOMPLETE => 'not complete', COMPLETION_COMPLETE => 'complete',
                   COMPLETION_COMPLETE_PASS => 'complete (passed)', COMPLETION_COMPLETE_FAIL => 'complete (failed)'];
        $activities = [];
        $modinfo = get_fast_modinfo($course, $user->id);
        foreach ($modinfo->get_cms() as $cm) {
            if (!$cm->uservisible || $cm->completion == COMPLETION_TRACKING_NONE) {
                continue;
            }
            $details = \core_completion\cm_completion_details::get_instance($cm, $user->id);
            $state = $details->get_overall_completion();
            $requirements = [];
            foreach ($details->get_details() as $detail) {
                $requirements[] = [
                    'requirement' => strip_tags($detail->description),
                    'met' => in_array($detail->status, [COMPLETION_COMPLETE, COMPLETION_COMPLETE_PASS], true),
                ];
            }
            $activities[] = [
                'activity' => html_entity_decode($cm->get_formatted_name(), ENT_QUOTES | ENT_HTML5, 'UTF-8'),
                'type' => $cm->modfullname,
                'section' => html_entity_decode(get_section_name($course, $cm->sectionnum), ENT_QUOTES | ENT_HTML5, 'UTF-8'),
                'status' => $states[$state] ?? 'not complete',
                'marked_by' => $details->is_manual() ? 'the student' : 'Moodle, automatically',
                'requirements' => $requirements,
            ];
        }
        $done = count(array_filter($activities, function($a) {
            return strpos($a['status'], 'complete') === 0;
        }));
        $out['activities_summary'] = "{$done} of " . count($activities) . ' tracked activities complete';
        $out['activities'] = $activities;
        return $out;
    }

    /** The student's grade report: each item they may see, the course total, and a summary. */
    private static function grades(\stdClass $course, \stdClass $user): array {
        $result = self::call('gradereport_user_get_grade_items', ['courseid' => $course->id, 'userid' => $user->id]);
        if (isset($result['error'])) {
            return $result;
        }
        $items = [];
        $total = null;
        $graded = $ungraded = 0;
        $weightgraded = $weightleft = 0.0;
        $hasweights = false;
        foreach ($result['usergrades'][0]['gradeitems'] ?? [] as $item) {
            if (!empty($item['gradeishidden']) || !empty($item['gradehiddenbydate'])) {
                continue;  // belt and braces: the report already withholds these from the student
            }
            $isgraded = isset($item['graderaw']) && $item['graderaw'] !== null && $item['graderaw'] !== '';
            $row = [
                'item' => strip_tags((string) ($item['itemname'] ?? '')),
                'type' => $item['itemmodule'] ?? $item['itemtype'] ?? '',
                'grade' => $isgraded ? strip_tags((string) ($item['gradeformatted'] ?? '')) : 'not graded yet',
                'range' => strip_tags((string) ($item['rangeformatted'] ?? '')),
                'percentage' => strip_tags((string) ($item['percentageformatted'] ?? '')),
                'letter' => strip_tags((string) ($item['lettergradeformatted'] ?? '')),
                'weight' => strip_tags((string) ($item['weightformatted'] ?? '')),
                'contribution_to_total' => strip_tags((string) ($item['contributiontocoursetotal'] ?? '')),
                'feedback' => \core_text::substr(trim(strip_tags((string) ($item['feedback'] ?? ''))), 0, self::FEEDBACK_CHARS),
            ];
            $row = array_filter($row, function($v) {
                return $v !== '' && $v !== '-';
            });
            if (($item['itemtype'] ?? '') === 'course') {
                $row['item'] = 'Course total';
                $total = $row;
                continue;
            }
            if (($item['itemtype'] ?? '') === 'category') {
                $row['item'] = ($row['item'] ?? 'Category') . ' (category total)';
            } else if ($isgraded) {
                $graded++;
            } else {
                $ungraded++;
            }
            if (isset($item['weightraw']) && is_numeric($item['weightraw']) && ($item['itemtype'] ?? '') !== 'category') {
                $hasweights = true;
                if ($isgraded) {
                    $weightgraded += (float) $item['weightraw'];
                } else {
                    $weightleft += (float) $item['weightraw'];
                }
            }
            $items[] = $row;
        }
        $summary = ['graded_items' => $graded, 'not_yet_graded_items' => $ungraded];
        if ($hasweights) {
            $summary['weight_already_graded'] = round($weightgraded * 100, 1) . '%';
            $summary['weight_still_to_come'] = round($weightleft * 100, 1) . '%';
        }
        return ['course_total' => $total ?? ['note' => 'The course total is not shown to students in this course.'],
                'summary' => $summary, 'items' => $items];
    }

    /** What the student still has to do, with due dates (their own extensions and overrides included). */
    private static function deadlines(\stdClass $course, \stdClass $user): array {
        $result = self::call('core_calendar_get_action_events_by_course', [
            'courseid' => $course->id, 'timesortfrom' => time() - 60 * DAYSECS, 'limitnum' => 50,
        ]);
        if (isset($result['error'])) {
            return $result;
        }
        $now = time();
        $events = [];
        foreach ($result['events'] ?? [] as $e) {
            $events[] = array_filter([
                'activity' => strip_tags((string) ($e['activityname'] ?? $e['name'] ?? '')),
                'event' => strip_tags((string) ($e['name'] ?? '')),
                'type' => $e['modulename'] ?? '',
                'date' => userdate((int) $e['timesort']),
                'overdue' => !empty($e['overdue']) || (int) $e['timesort'] < $now,
                'to_do' => strip_tags((string) ($e['action']['name'] ?? '')),
                'can_do_now' => !empty($e['action']['actionable']),
            ], function($v) {
                return $v !== '' && $v !== null;
            });
        }
        return ['now' => userdate($now), 'note' => 'Only things the student still has to do are listed; '
                . 'submitted or finished work does not appear.', 'to_do' => $events];
    }

    /** Call a Moodle web service function as the current user (the student). */
    private static function call(string $function, array $args): array {
        global $CFG;
        if (!class_exists('\core_external\external_api')) {
            require_once($CFG->libdir . '/externallib.php');  // Moodle 4.0-4.1
        }
        $api = class_exists('\core_external\external_api') ? '\core_external\external_api' : '\external_api';
        $response = $api::call_external_function($function, $args);
        if (!empty($response['error'])) {
            $message = $response['exception']->message ?? 'not available';
            return ['error' => 'Moodle could not provide this: ' . strip_tags((string) $message)];
        }
        return $response['data'];
    }
}
