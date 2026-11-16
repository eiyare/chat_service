<?php
defined('MOODLE_INTERNAL') || die();

// Moodle 4.4+. Older versions ignore this file and call local_saaschat_before_footer().
$callbacks = [
    [
        'hook' => \core\hook\output\before_footer_html_generation::class,
        'callback' => [\local_saaschat\hook_callbacks::class, 'before_footer'],
    ],
];
