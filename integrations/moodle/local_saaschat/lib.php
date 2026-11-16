<?php
defined('MOODLE_INTERNAL') || die();

/**
 * The widget's <script> tag, with the signed-in student signed by the shared code.
 *
 * Token: base64url(JSON {sub, name, exp}) . "." . hex HMAC-SHA256(shared code, payload).
 * Guests and visitors who aren't logged in get no token and are greeted as "Guest".
 */
function local_saaschat_widget_html(): string {
    global $USER;
    if (during_initial_install() || (defined('AJAX_SCRIPT') && AJAX_SCRIPT) || CLI_SCRIPT) {
        return '';
    }
    $server = rtrim((string) get_config('local_saaschat', 'serverurl'), '/');
    $key = (string) get_config('local_saaschat', 'widgetkey');
    if ($server === '' || $key === '') {
        return '';
    }
    $attributes = ['src' => $server . '/static/widget/embed.js', 'data-key' => $key, 'async' => 'async'];
    $code = (string) get_config('local_saaschat', 'sharedcode');
    if ($code !== '' && isloggedin() && !isguestuser()) {
        $payload = rtrim(strtr(base64_encode(json_encode([
            'sub' => (int) $USER->id,
            'name' => fullname($USER),
            'exp' => time() + 12 * HOURSECS,
        ])), '+/', '-_'), '=');
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
