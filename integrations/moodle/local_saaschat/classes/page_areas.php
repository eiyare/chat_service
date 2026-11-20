<?php
namespace local_saaschat;

defined('MOODLE_INTERNAL') || die();

/**
 * Which Moodle pages show the assistant: the areas ticked in the plugin settings,
 * minus any page types listed under "Hide on these page types".
 */
class page_areas {
    /** Quiz attempt pages, checked before the layouts because they use the "incourse" layout. */
    const QUIZ_PAGETYPES = ['mod-quiz-attempt', 'mod-quiz-summary'];

    /** Area => the page layouts it covers. Layouts not listed here belong to "other". */
    const LAYOUTS = [
        'frontpage' => ['frontpage'],
        'dashboard' => ['mydashboard', 'mycourses'],
        'coursecategory' => ['coursecategory'],
        'course' => ['course'],
        'incourse' => ['incourse'],
        'quiz' => ['secure'],
        'profile' => ['mypublic'],
        'report' => ['report'],
        'admin' => ['admin'],
        'login' => ['login'],
        'other' => [],
    ];

    /** Checkbox choices for settings.php, all ticked by default. */
    public static function choices(): array {
        $choices = [];
        foreach (array_keys(self::LAYOUTS) as $area) {
            $choices[$area] = get_string('pagearea_' . $area, 'local_saaschat');
        }
        return $choices;
    }

    public static function defaults(): array {
        return array_fill_keys(array_keys(self::LAYOUTS), 1);
    }

    /** The area a page belongs to. */
    public static function area(string $pagelayout, string $pagetype): string {
        if (in_array($pagetype, self::QUIZ_PAGETYPES, true)) {
            return 'quiz';
        }
        foreach (self::LAYOUTS as $area => $layouts) {
            if (in_array($pagelayout, $layouts, true)) {
                return $area;
            }
        }
        return 'other';
    }

    /** Whether the assistant is shown on a page with this layout and page type. */
    public static function shown(string $pagelayout, string $pagetype): bool {
        $enabled = get_config('local_saaschat', 'pageareas');
        // Not saved yet (plugin just upgraded): every area, as before the setting existed.
        if ($enabled !== false) {
            $enabled = $enabled === '' ? [] : explode(',', $enabled);
            if (!in_array(self::area($pagelayout, $pagetype), $enabled, true)) {
                return false;
            }
        }
        $hidden = preg_split('/\R/', (string) get_config('local_saaschat', 'hiddenpagetypes'));
        foreach ($hidden as $pattern) {
            $pattern = trim($pattern);
            if ($pattern === '') {
                continue;
            }
            if (substr($pattern, -1) === '*'
                    ? strpos($pagetype, substr($pattern, 0, -1)) === 0
                    : $pagetype === $pattern) {
                return false;
            }
        }
        return true;
    }
}
