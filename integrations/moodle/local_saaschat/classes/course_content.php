<?php
namespace local_saaschat;

defined('MOODLE_INTERNAL') || die();

/**
 * What saaschat gets from one course: an overview with the schedule, one document per
 * activity (description, plus Page/Book content and forum posts), and the files in File
 * and Folder activities.
 *
 * Only what every student can see is sent: visible course sections and activities shown
 * on the course page, with no access restrictions. Quiz questions are never sent.
 * Forum posts leave out private replies, Q&A forums, separate-group discussions and timed
 * posts outside their display period.
 */
class course_content {
    /** Courses chosen in the plugin settings. */
    const SYNC_OFF = 0;
    const SYNC_ALL = 1;
    const SYNC_LISTED = 2;

    /** @var \stdClass */
    private $course;
    /** @var \course_modinfo */
    private $modinfo;
    /** @var string[] lowercase file extensions saaschat accepts */
    private $extensions;
    /** @var int */
    private $maxbytes;

    public function __construct(\stdClass $course, array $extensions, int $maxbytes) {
        $this->course = $course;
        $this->modinfo = get_fast_modinfo($course, 0);
        $this->extensions = array_map('strtolower', $extensions);
        $this->maxbytes = $maxbytes;
    }

    /** IDs of the courses to send, per the plugin settings (visible courses only). */
    public static function course_ids(): array {
        global $DB;
        $mode = (int) get_config('local_saaschat', 'coursesync');
        if ($mode === self::SYNC_ALL) {
            return array_map('intval', $DB->get_fieldset_select('course', 'id', 'id <> ? AND visible = 1', [SITEID]));
        }
        if ($mode === self::SYNC_LISTED) {
            $ids = self::listed_ids();
            if (!$ids) {
                return [];
            }
            [$in, $params] = $DB->get_in_or_equal($ids);
            return array_map('intval', $DB->get_fieldset_select('course', 'id', "id $in AND visible = 1", $params));
        }
        return [];
    }

    /** Whether the course's content is sent to saaschat (so students on its pages get course answers). */
    public static function is_synced(\stdClass $course): bool {
        if ((int) $course->id === (int) SITEID || empty($course->visible)) {
            return false;
        }
        $mode = (int) get_config('local_saaschat', 'coursesync');
        return $mode === self::SYNC_ALL || ($mode === self::SYNC_LISTED && in_array((int) $course->id, self::listed_ids(), true));
    }

    private static function listed_ids(): array {
        $raw = (string) get_config('local_saaschat', 'courseids');
        return array_values(array_unique(array_filter(array_map('intval', preg_split('/[\s,;]+/', $raw)))));
    }

    /**
     * The course's items, keyed by item key. Each is
     * ['name', 'version', 'text'] or ['name', 'version', 'file' => stored_file, 'filename'].
     */
    public function items(): array {
        $items = [];
        $overview = $this->overview();
        $items['overview'] = $this->text_item($overview[0], $overview[1]);
        foreach ($this->modinfo->get_cms() as $cm) {
            if (!$this->shown($cm)) {
                continue;
            }
            $text = $this->activity_text($cm);
            if ($text !== null) {
                $items['cm:' . $cm->id] = $this->text_item($cm->get_formatted_name() . ' (' . $cm->modfullname . ')', $text);
            }
            foreach ($this->activity_files($cm) as $file) {
                $items['file:' . $cm->id . ':' . $file->get_id()] = [
                    'name' => $file->get_filename() . ' (' . $cm->get_formatted_name() . ')',
                    'version' => sha1($file->get_contenthash() . '|' . $file->get_filename() . '|' . $cm->name),
                    'file' => $file,
                    'filename' => $file->get_filename(),
                ];
            }
        }
        return $items;
    }

    private function text_item(string $name, string $text): array {
        $text = \core_text::str_max_bytes($text, $this->maxbytes);
        return ['name' => \core_text::substr($name, 0, 250), 'version' => sha1($name . "\n" . $text), 'text' => $text];
    }

    /** Visible to every student: section and activity visible on the course page, no restrictions. */
    private function shown(\cm_info $cm): bool {
        if (!$cm->visible || !$cm->visibleoncoursepage || !empty($cm->deletioninprogress) || !empty($cm->availability)) {
            return false;
        }
        $section = $this->modinfo->get_section_info($cm->sectionnum);
        return $section && $section->visible && empty($section->availability);
    }

    /** [document name, Markdown text]: the course, its sections, activities and their dates. */
    private function overview(): array {
        $course = $this->course;
        $name = format_string($course->fullname, true, ['context' => \context_course::instance($course->id)]);
        $lines = ["# {$name}: course overview and schedule", ''];
        $facts = ["Course: {$name} ({$course->shortname})."];
        if (!empty($course->startdate)) {
            $facts[] = 'Starts: ' . $this->date($course->startdate) . '.';
        }
        if (!empty($course->enddate)) {
            $facts[] = 'Ends: ' . $this->date($course->enddate) . '.';
        }
        $lines[] = implode(' ', $facts);
        $count = $this->activity_count();
        if ($count !== '') {
            $lines[] = $count;
        }
        $summary = trim(content_to_text((string) $course->summary, (int) $course->summaryformat));
        if ($summary !== '') {
            $lines[] = '';
            $lines[] = $summary;
        }
        foreach ($this->modinfo->get_section_info_all() as $section) {
            if (!$section->visible || !empty($section->availability)) {
                continue;
            }
            $body = [];
            $summary = trim(content_to_text((string) $section->summary, (int) $section->summaryformat));
            if ($summary !== '') {
                $body[] = $summary;
            }
            foreach ($this->modinfo->sections[$section->section] ?? [] as $cmid) {
                $cm = $this->modinfo->get_cm($cmid);
                if (!$this->shown($cm)) {
                    continue;
                }
                if ($cm->modname === 'label') {  // labels are course-page text: keep them here
                    $text = $this->intro($cm);
                    if ($text !== '') {
                        $body[] = $text;
                    }
                    continue;
                }
                $dates = $this->dates($cm);
                $body[] = '- ' . $cm->get_formatted_name() . ' (' . $cm->modfullname . ')' . ($dates ? ': ' . $dates : '');
            }
            if ($body) {
                $lines[] = '';
                $lines[] = '## ' . get_section_name($course, $section);
                $lines = array_merge($lines, $body);
            }
        }
        return ["{$name}: course overview and schedule", implode("\n", $lines)];
    }

    /**
     * "This course has 12 activities and resources: 4 Assignments, 3 Quizzes, …", so questions
     * about how many there are can be answered from one passage (labels aren't counted).
     */
    private function activity_count(): string {
        $counts = [];
        foreach ($this->modinfo->get_cms() as $cm) {
            if ($cm->modname !== 'label' && $this->shown($cm)) {
                $counts[$cm->modname] = ($counts[$cm->modname] ?? 0) + 1;
            }
        }
        if (!$counts) {
            return '';
        }
        arsort($counts);
        $parts = [];
        foreach ($counts as $modname => $n) {
            $type = get_string($n === 1 ? 'modulename' : 'modulenameplural', 'mod_' . $modname);
            $parts[] = "{$n} {$type}";
        }
        $total = array_sum($counts);
        return "This course has {$total} " . ($total === 1 ? 'activity or resource' : 'activities and resources')
            . ': ' . implode(', ', $parts) . '.';
    }

    /** Markdown for one activity, or null when it has nothing beyond what the overview says. */
    private function activity_text(\cm_info $cm): ?string {
        global $DB;
        if ($cm->modname === 'label') {
            return null;
        }
        $parts = [];
        $intro = $this->intro($cm);
        if ($intro !== '') {
            $parts[] = $intro;
        }
        switch ($cm->modname) {
            case 'page':
                $page = $DB->get_record('page', ['id' => $cm->instance], 'id, content, contentformat');
                if ($page) {
                    $parts[] = $this->to_text($page->content, $page->contentformat);
                }
                break;
            case 'book':
                $chapters = $DB->get_records('book_chapters', ['bookid' => $cm->instance, 'hidden' => 0], 'pagenum',
                    'id, title, content, contentformat');
                foreach ($chapters as $chapter) {
                    $parts[] = '## ' . format_string($chapter->title) . "\n" . $this->to_text($chapter->content, $chapter->contentformat);
                }
                break;
            case 'assign':
                $assign = $DB->get_record('assign', ['id' => $cm->instance]);
                if ($assign && !empty($assign->activity)) {  // "Activity instructions" (Moodle 4.0+)
                    $parts[] = "## Instructions\n" . $this->to_text($assign->activity, $assign->activityformat ?? FORMAT_HTML);
                }
                break;
            case 'url':
                $url = $DB->get_field('url', 'externalurl', ['id' => $cm->instance]);
                if ($url) {
                    $parts[] = 'Web address: ' . $url;
                }
                break;
            case 'forum':
                $posts = $this->forum_posts($cm);
                if ($posts !== '') {
                    $parts[] = $posts;
                }
                break;
        }
        $parts = array_filter(array_map('trim', $parts), 'strlen');
        if (!$parts) {
            return null;
        }
        $section = get_section_name($this->course, $this->modinfo->get_section_info($cm->sectionnum));
        $facts = ["Type: {$cm->modfullname}. Section: {$section}."];
        $dates = $this->dates($cm);
        if ($dates) {
            $facts[] = $dates . '.';
        }
        if ($cm->url) {
            $facts[] = 'Link: ' . $cm->url->out(false);
        }
        return '# ' . $cm->get_formatted_name() . "\n\n" . implode(' ', $facts) . "\n\n" . implode("\n\n", $parts);
    }

    private function intro(\cm_info $cm): string {
        global $DB;
        if (!plugin_supports('mod', $cm->modname, FEATURE_MOD_INTRO, true)) {
            return '';
        }
        $record = $DB->get_record($cm->modname, ['id' => $cm->instance], 'id, intro, introformat');
        return $record ? $this->to_text($record->intro, $record->introformat) : '';
    }

    /** Forum discussions every student can read, newest first, as Markdown. */
    private function forum_posts(\cm_info $cm): string {
        global $DB;
        $forum = $DB->get_record('forum', ['id' => $cm->instance], 'id, type');
        if (!$forum || $forum->type === 'qanda') {  // Q&A: students must post before they see others' posts
            return '';
        }
        $groupmode = groups_get_activity_groupmode($cm, $this->course);
        $now = time();
        $discussions = $DB->get_records('forum_discussions', ['forum' => $forum->id], 'timemodified DESC',
            'id, name, groupid, timestart, timeend');
        $out = [];
        $bytes = 0;
        foreach ($discussions as $discussion) {
            if ($groupmode == SEPARATEGROUPS && (int) $discussion->groupid !== -1) {
                continue;
            }
            if ((!empty($discussion->timestart) && $discussion->timestart > $now)
                    || (!empty($discussion->timeend) && $discussion->timeend < $now)) {
                continue;
            }
            $posts = $DB->get_records_select('forum_posts', 'discussion = ? AND privatereplyto = 0 AND deleted = 0',
                [$discussion->id], 'created ASC', 'id, userid, subject, message, messageformat, created');
            if (!$posts) {
                continue;
            }
            $names = $this->user_names(array_unique(array_column($posts, 'userid')));
            $text = ['## Discussion: ' . format_string($discussion->name)];
            foreach ($posts as $post) {
                $who = $names[$post->userid] ?? get_string('unknownuser');
                $text[] = "{$who}, " . $this->date($post->created) . ":\n" . $this->to_text($post->message, $post->messageformat);
            }
            $block = implode("\n\n", $text);
            $bytes += strlen($block);
            if ($bytes > $this->maxbytes) {
                break;  // the newest discussions fit; older ones are left out
            }
            $out[] = $block;
        }
        return implode("\n\n", $out);
    }

    private function user_names(array $ids): array {
        global $DB;
        if (!$ids) {
            return [];
        }
        $fields = 'id, ' . implode(', ', \core_user\fields::get_name_fields());
        $users = $DB->get_records_list('user', 'id', $ids, '', $fields);
        return array_map('fullname', $users);
    }

    /** Files of File and Folder activities that saaschat accepts. */
    private function activity_files(\cm_info $cm): array {
        if (!in_array($cm->modname, ['resource', 'folder'], true)) {
            return [];
        }
        $files = get_file_storage()->get_area_files($cm->context->id, 'mod_' . $cm->modname, 'content', 0,
            'sortorder DESC, id ASC', false);
        return array_values(array_filter($files, function(\stored_file $file) {
            $ext = strtolower(pathinfo($file->get_filename(), PATHINFO_EXTENSION));
            return in_array($ext, $this->extensions, true) && $file->get_filesize() > 0
                && $file->get_filesize() <= $this->maxbytes;
        }));
    }

    /** "Opens: …; Due: …" from the activity's dates (the same for every student, ignoring overrides). */
    private function dates(\cm_info $cm): string {
        try {
            $dates = \core\activity_dates::get_dates_for_module($cm, 0);
        } catch (\Throwable $e) {
            return '';
        }
        $out = [];
        foreach ($dates as $date) {
            if (!empty($date['timestamp'])) {
                $out[] = rtrim((string) $date['label'], ': ') . ': ' . $this->date($date['timestamp']);
            }
        }
        return implode('; ', $out);
    }

    private function date(int $timestamp): string {
        return userdate($timestamp, get_string('strftimedatetime', 'langconfig'), \core_date::get_server_timezone());
    }

    private function to_text($content, $format): string {
        return trim(content_to_text((string) $content, (int) $format));
    }
}
