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
    $settings->add(new admin_setting_configpasswordunmask('local_saaschat/sharedcode',
        get_string('sharedcode', 'local_saaschat'), get_string('sharedcode_desc', 'local_saaschat'), ''));
}
