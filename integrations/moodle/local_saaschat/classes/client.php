<?php
namespace local_saaschat;

defined('MOODLE_INTERNAL') || die();

/**
 * Server-to-server calls to saaschat for the widget, authenticated with the shared code.
 */
class client {
    /** @var string e.g. https://saaschat.tech/w/<key>/ */
    private $base;
    /** @var string */
    private $code;

    private function __construct(string $base, string $code) {
        $this->base = $base;
        $this->code = $code;
    }

    /** The configured client, or null if the plugin isn't set up. */
    public static function configured(): ?self {
        global $CFG;
        require_once($CFG->libdir . '/filelib.php');
        $server = rtrim((string) get_config('local_saaschat', 'serverurl'), '/');
        $key = (string) get_config('local_saaschat', 'widgetkey');
        $code = (string) get_config('local_saaschat', 'sharedcode');
        if ($server === '' || $key === '' || $code === '') {
            return null;
        }
        return new self($server . '/w/' . rawurlencode($key) . '/', $code);
    }

    /** GET; returns [HTTP status, decoded JSON or null]. */
    public function get(string $path): array {
        $curl = $this->curl();
        $body = $curl->get($this->base . $path);
        return $this->result($curl, $body);
    }

    /** POST a JSON body. */
    public function post_json(string $path, array $data): array {
        $curl = $this->curl(['Content-Type: application/json']);
        $body = $curl->post($this->base . $path, json_encode($data));
        return $this->result($curl, $body);
    }

    /** POST multipart form fields; values may be stored_file or CURLFile objects. */
    public function post_form(string $path, array $fields): array {
        $curl = $this->curl();
        $body = $curl->post($this->base . $path, $fields);
        return $this->result($curl, $body);
    }

    private function curl(array $headers = []): \curl {
        $curl = new \curl();
        $curl->setHeader(array_merge(['Authorization: Bearer ' . $this->code, 'Accept: application/json'], $headers));
        return $curl;
    }

    private function result(\curl $curl, $body): array {
        $status = (int) ($curl->get_info()['http_code'] ?? 0);
        return [$status, json_decode((string) $body, true)];
    }
}
