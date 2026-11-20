<?php
defined('MOODLE_INTERNAL') || die();

/**
 * The widget's <script> tag, with the signed-in student signed by the shared code.
 *
 * Token: base64url(JSON {sub, name, exp[, course[, cm]]}) . "." . hex HMAC-SHA256(shared code, payload).
 * Guests and visitors who aren't logged in get no token and are greeted as "Guest".
 * Nothing is added on pages whose area or page type is turned off in the plugin settings.
 * On the pages of a course whose content is sent to saaschat, and which the student can
 * access, the token names the course: answers then come from that course only. On an
 * activity's pages it also names the activity, so "this module" means that one.
 */
function local_saaschat_widget_html(): string {
    global $USER, $PAGE;
    if (during_initial_install() || (defined('AJAX_SCRIPT') && AJAX_SCRIPT) || CLI_SCRIPT) {
        return '';
    }
    $server = rtrim((string) get_config('local_saaschat', 'serverurl'), '/');
    $key = (string) get_config('local_saaschat', 'widgetkey');
    if ($server === '' || $key === '') {
        return '';
    }
    if (!\local_saaschat\page_areas::shown((string) $PAGE->pagelayout, (string) $PAGE->pagetype)) {
        return '';
    }
    $attributes = ['src' => $server . '/static/widget/embed.js', 'data-key' => $key, 'async' => 'async'];
    $code = (string) get_config('local_saaschat', 'sharedcode');
    if ($code !== '' && isloggedin() && !isguestuser()) {
        $data = [
            'sub' => (int) $USER->id,
            'name' => fullname($USER),
            'exp' => time() + 12 * HOURSECS,
        ];
        $course = $PAGE->course ?? null;
        if ($course && \local_saaschat\course_content::is_synced($course) && can_access_course($course, null, '', true)) {
            $data['course'] = (int) $course->id;
            $cm = $PAGE->cm ?? null;
            if ($cm && (int) $cm->course === (int) $course->id && $cm->uservisible) {
                $data['cm'] = (int) $cm->id;
            }
        }
        $payload = rtrim(strtr(base64_encode(json_encode($data)), '+/', '-_'), '=');
        $attributes['data-user'] = $payload . '.' . hash_hmac('sha256', $payload, $code);
    }
    return html_writer::tag('script', '', $attributes);
}

/**
 * Moodle 4.0-4.3. From 4.4 the hook in db/hooks.php is used instead.
 */
function local_saaschat_before_footer() {
    return local_saaschat_widget_html();
}
