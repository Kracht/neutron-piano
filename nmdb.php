<?php
/* Neutron Piano NMDB proxy (PHP variant of serve.py's /api/nmdb).
 *
 * NMDB's NEST interface sends no CORS headers and answers with an HTML page
 * wrapping a semicolon-separated ASCII table, so the browser cannot read it.
 * This script fetches it, parses the table and returns JSON. The upstream URLs
 * are fixed; the only input is `days` (clamped to 1..7), so it is not an open
 * proxy. Responses are cached for 55 s in the system temp directory.
 *
 * NEST runs in Kiel and, as a mirror, in Paris. The source that answered last
 * is asked first, so an outage costs one connect timeout, not one per fetch.
 * Every 30 minutes the main site gets the first try again.
 */

const STATIONS = [
    'TERA', 'SOPO', 'DOMC', 'THUL', 'INVK', 'FSMT', 'NAIN', 'PWNK', 'TXBY',
    'APTY', 'OULU', 'KERG', 'YKTK', 'KIEL2', 'NEWK', 'DRBS', 'IRKT', 'LMKS',
    'JUNG1', 'BKSN', 'ROME', 'AATB', 'CALM', 'MXCO', 'ATHN',
];
const SOURCES = [
    'Kiel' => 'https://www.nmdb.eu/nest/draw_graph.php',
    'Paris' => 'https://previ.obspm.fr/nest/draw_graph.php',
];
const CACHE_SECONDS = 55;
const RETRY_MAIN_SECONDS = 1800;
// stale data beat no data, but not for long: after that the page shows the outage
const STALE_MAX_SECONDS = 1800;

function fetch_upstream(string $url): string
{
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_CONNECTTIMEOUT => 10,
            CURLOPT_TIMEOUT => 40,
            CURLOPT_USERAGENT => 'neutron-piano',
        ]);
        $body = curl_exec($ch);
        $code = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        curl_close($ch);
        if ($body === false || $code !== 200) {
            throw new RuntimeException("upstream HTTP $code");
        }
        return $body;
    }
    $ctx = stream_context_create(['http' => ['timeout' => 40, 'user_agent' => 'neutron-piano']]);
    $body = @file_get_contents($url, false, $ctx);
    if ($body === false) {
        throw new RuntimeException('upstream fetch failed (no curl, allow_url_fopen off?)');
    }
    return $body;
}

function fetch_nmdb(int $days): array
{
    $q = 'formchk=1';
    foreach (STATIONS as $s) {
        $q .= '&' . rawurlencode('stations[]') . '=' . $s;
    }
    $q .= '&' . http_build_query([
        'tabchoice' => 'revori',
        'dtype' => 'corr_for_efficiency',
        'tresolution' => 1,
        'force' => 1,
        'yunits' => 0,
        'date_choice' => 'last',
        'last_days' => $days,
        'output' => 'ascii',
    ]);
    // try the source that worked last, unless it is a mirror and the main site is due a retry
    $memo = sys_get_temp_dir() . '/neutron-piano-nmdb-source.txt';
    $names = array_keys(SOURCES);
    $last = is_file($memo) ? trim((string)file_get_contents($memo)) : '';
    if (isset(SOURCES[$last]) && ($last === $names[0] || time() - filemtime($memo) < RETRY_MAIN_SECONDS)) {
        $names = array_values(array_unique(array_merge([$last], $names)));
    }
    $errors = [];
    foreach ($names as $name) {
        try {
            $text = fetch_upstream(SOURCES[$name] . '?' . $q);
            if (!preg_match('/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d;/m', $text)) {
                throw new RuntimeException('no data rows');
            }
            if ($name !== $last || time() - filemtime($memo) >= RETRY_MAIN_SECONDS) {
                file_put_contents($memo, $name, LOCK_EX);
            }
            break;
        } catch (Throwable $e) {
            $errors[] = "$name: " . $e->getMessage();
            $text = null;
        }
    }
    if ($text === null) {
        throw new RuntimeException(implode('; ', $errors));
    }

    $stations = [];
    $rows = [];
    foreach (preg_split('/\r?\n/', $text) as $line) {
        if (preg_match('/^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d;/', $line)) {
            $parts = explode(';', $line);
            // NMDB times are UTC
            $row = [str_replace(' ', 'T', $parts[0]) . 'Z'];
            foreach (array_slice($parts, 1) as $p) {
                $p = trim($p);
                $row[] = $p === 'null' ? null : (float)$p;
            }
            $rows[] = $row;
        } elseif (!$rows && preg_match('/^\s+([A-Z0-9]{3,6}\s*)+$/', $line)) {
            // NMDB drops stations without data; the header line is the truth
            $stations = preg_split('/\s+/', trim($line));
        }
    }
    if ($rows && count($rows[0]) - 1 !== count($stations)) {
        throw new RuntimeException('NMDB header/column mismatch');
    }
    return ['stations' => $stations, 'rows' => $rows, 'fetched' => time(), 'source' => $name];
}

$days = max(1, min(7, (int)($_GET['days'] ?? 1)));
$cache = sys_get_temp_dir() . "/neutron-piano-nmdb-$days.json";

$fresh = is_file($cache) && time() - filemtime($cache) < CACHE_SECONDS;
if ($fresh) {
    $body = file_get_contents($cache);
} else {
    try {
        $body = json_encode(fetch_nmdb($days));
        file_put_contents($cache, $body, LOCK_EX);
    } catch (Throwable $e) {
        if (is_file($cache) && time() - filemtime($cache) < STALE_MAX_SECONDS) {
            $body = file_get_contents($cache);
        } else {
            http_response_code(502);
            header('Content-Type: text/plain; charset=utf-8');
            echo 'NMDB: ' . $e->getMessage();
            exit;
        }
    }
}

header('Content-Type: application/json');
header('Cache-Control: no-store');
echo $body;
