<?php
// saaschat for Moodle: adds the saaschat assistant to the chosen pages, greets signed-in
// students by name, answers from the course a student is in, and can keep a copy of the
// workspace's document passages.

defined('MOODLE_INTERNAL') || die();

$plugin->component = 'local_saaschat';
$plugin->version   = 2026112000;
$plugin->requires  = 2022041900; // Moodle 4.0.
$plugin->maturity  = MATURITY_BETA;
$plugin->release   = '0.3.0';
