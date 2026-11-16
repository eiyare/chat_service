<?php
namespace local_saaschat;

defined('MOODLE_INTERNAL') || die();

class hook_callbacks {
    public static function before_footer(\core\hook\output\before_footer_html_generation $hook): void {
        global $CFG;
        require_once($CFG->dirroot . '/local/saaschat/lib.php');
        $hook->add_html(local_saaschat_widget_html());
    }
}
