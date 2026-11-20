<?php
defined('MOODLE_INTERNAL') || die();

if ($hassiteconfig) {
    $settings = new admin_settingpage('local_saaschat', get_string('pluginname', 'local_saaschat'));
    $ADMIN->add('localplugins', $settings);

    $settings->add(new admin_setting_configtext('local_saaschat/serverurl',
        get_string('serverurl', 'local_saaschat'), get_string('serverurl_desc', 'local_saaschat'),
        'https://saaschat.tech', PARAM_URL));
    $settings->add(new admin_setting_configtext('local_saaschat/widgetkey',
        get_string('widgetkey', 'local_saaschat'), get_string('widgetkey_desc', 'local_saaschat'),
        '', PARAM_ALPHANUMEXT));
    $settings->add(new admin_setting_configmulticheckbox('local_saaschat/pageareas',
        get_string('pageareas', 'local_saaschat'), get_string('pageareas_desc', 'local_saaschat'),
        \local_saaschat\page_areas::defaults(), \local_saaschat\page_areas::choices()));
    $settings->add(new admin_setting_configtextarea('local_saaschat/hiddenpagetypes',
        get_string('hiddenpagetypes', 'local_saaschat'), get_string('hiddenpagetypes_desc', 'local_saaschat'),
        '', PARAM_TEXT));
    $settings->add(new admin_setting_configpasswordunmask('local_saaschat/sharedcode',
        get_string('sharedcode', 'local_saaschat'), get_string('sharedcode_desc', 'local_saaschat'), ''));
    $settings->add(new admin_setting_configselect('local_saaschat/coursesync',
        get_string('coursesync', 'local_saaschat'), get_string('coursesync_desc', 'local_saaschat'),
        \local_saaschat\course_content::SYNC_OFF, [
            \local_saaschat\course_content::SYNC_OFF => get_string('coursesync_off', 'local_saaschat'),
            \local_saaschat\course_content::SYNC_ALL => get_string('coursesync_all', 'local_saaschat'),
            \local_saaschat\course_content::SYNC_LISTED => get_string('coursesync_listed', 'local_saaschat'),
        ]));
    $settings->add(new admin_setting_configtext('local_saaschat/courseids',
        get_string('courseids', 'local_saaschat'), get_string('courseids_desc', 'local_saaschat'), '', PARAM_SEQUENCE));
}
