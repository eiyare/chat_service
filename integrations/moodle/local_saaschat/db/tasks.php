<?php
defined('MOODLE_INTERNAL') || die();

$tasks = [
    [
        'classname' => \local_saaschat\task\sync_chunks::class,
        'blocking' => 0,
        'minute' => 'R',
        'hour' => '*',
        'day' => '*',
        'month' => '*',
        'dayofweek' => '*',
    ],
    [
        'classname' => \local_saaschat\task\sync_courses::class,
        'blocking' => 0,
        'minute' => 'R',
        'hour' => '*',
        'day' => '*',
        'month' => '*',
        'dayofweek' => '*',
    ],
];
