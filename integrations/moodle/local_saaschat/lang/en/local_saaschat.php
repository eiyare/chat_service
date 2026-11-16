<?php
defined('MOODLE_INTERNAL') || die();

$string['pluginname'] = 'saaschat';
$string['serverurl'] = 'saaschat address';
$string['serverurl_desc'] = 'Usually https://saaschat.tech.';
$string['widgetkey'] = 'Widget key';
$string['widgetkey_desc'] = 'From the data-key in the embed code on your saaschat workspace\'s Admin > Embed page. The assistant appears on every page once this is set.';
$string['sharedcode'] = 'Shared code';
$string['sharedcode_desc'] = 'Given to you by saaschat. Lets the assistant greet signed-in users by name, keep their conversations across devices, and copy document passages to Moodle.';
$string['task_sync_chunks'] = 'Copy saaschat document passages';
$string['syncfailed'] = 'Copying passages from saaschat failed (HTTP status {$a}). Check the widget key and shared code.';
$string['privacy:metadata:saaschat'] = 'The saaschat assistant answers questions from the workspace\'s documents.';
$string['privacy:metadata:saaschat:userid'] = 'The ID of the signed-in user, so their conversations follow them across devices.';
$string['privacy:metadata:saaschat:fullname'] = 'The user\'s full name, to greet them.';
$string['privacy:metadata:saaschat:messages'] = 'The questions the user asks the assistant.';
