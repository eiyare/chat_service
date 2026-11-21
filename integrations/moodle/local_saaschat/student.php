<?php
// Server-to-server: saaschat looks up a signed-in student's own grades, completion and
// deadlines while they are asking (see classes/student_data.php).

define('NO_MOODLE_COOKIES', true);
define('AJAX_SCRIPT', true);

require(__DIR__ . '/../../config.php');

\local_saaschat\student_data::handle();
