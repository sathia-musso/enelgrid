#!/bin/bash

# EnelGrid Migration Verification Script
# This script automatically detects your Home Assistant database configuration
# and verifies that the v1.1.0 migration was successful.

set -e  # Exit on error

echo "=========================================="
echo "EnelGrid v1.1.0 Migration Verification"
echo "=========================================="
echo ""
echo "🔍 Auto-detecting database configuration..."
echo ""

# Find Home Assistant configuration directory
if [ -d "/config" ]; then
    # HassOS / Docker installation
    CONFIG_DIR="/config"
elif [ -d "$HOME/.homeassistant" ]; then
    # Manual installation
    CONFIG_DIR="$HOME/.homeassistant"
elif [ -d "/usr/share/hassio/homeassistant" ]; then
    # Supervised installation
    CONFIG_DIR="/usr/share/hassio/homeassistant"
else
    echo "❌ ERROR: Could not find Home Assistant configuration directory"
    echo "   Please run this script from your Home Assistant system"
    exit 1
fi

echo "✓ Found Home Assistant config at: $CONFIG_DIR"

# Parse database configuration
CONFIG_FILE="$CONFIG_DIR/configuration.yaml"
DB_URL=""

if [ -f "$CONFIG_FILE" ]; then
    # Extract db_url from configuration.yaml
    DB_URL=$(grep -A 10 "^recorder:" "$CONFIG_FILE" | grep "db_url:" | sed 's/.*db_url:[ ]*//' | tr -d '"' | tr -d "'")
fi

# Determine database type and connection parameters
if [ -z "$DB_URL" ]; then
    # No db_url specified = SQLite (default)
    echo "✓ Detected: SQLite (default)"
    DB_TYPE="sqlite"
    DB_FILE="$CONFIG_DIR/home-assistant_v2.db"

    if [ ! -f "$DB_FILE" ]; then
        echo "❌ ERROR: SQLite database not found at $DB_FILE"
        exit 1
    fi
    echo "✓ Database file: $DB_FILE"

elif [[ "$DB_URL" =~ ^mysql:// ]] || [[ "$DB_URL" =~ ^mysql\+pymysql:// ]]; then
    # MySQL/MariaDB
    echo "✓ Detected: MySQL/MariaDB"
    DB_TYPE="mysql"

    # Parse MySQL URL: mysql://user:pass@host:port/database
    DB_URL_CLEAN=$(echo "$DB_URL" | sed 's/mysql+pymysql/mysql/' | sed 's/?.*$//')  # Remove query params

    # Extract components
    DB_USER=$(echo "$DB_URL_CLEAN" | sed -n 's#.*://\([^:]*\):.*#\1#p')
    DB_PASS=$(echo "$DB_URL_CLEAN" | sed -n 's#.*://[^:]*:\([^@]*\)@.*#\1#p')
    DB_HOST=$(echo "$DB_URL_CLEAN" | sed -n 's#.*@\([^:/]*\).*#\1#p')
    DB_PORT=$(echo "$DB_URL_CLEAN" | sed -n 's#.*:\([0-9]*\)/.*#\1#p')
    DB_NAME=$(echo "$DB_URL_CLEAN" | sed -n 's#.*/\([^?]*\).*#\1#p')

    # Default port if not specified
    if [ -z "$DB_PORT" ]; then
        DB_PORT="3306"
    fi

    # Handle host.docker.internal (Docker environments)
    if [ "$DB_HOST" = "host.docker.internal" ]; then
        DB_HOST="127.0.0.1"
    fi

    echo "✓ Connection: $DB_USER@$DB_HOST:$DB_PORT/$DB_NAME"

    # Test MySQL connection
    if ! command -v mysql &> /dev/null; then
        echo "❌ ERROR: mysql client not installed"
        echo "   Install with: apt-get install mysql-client (Debian/Ubuntu)"
        echo "             or: yum install mysql (CentOS/RHEL)"
        exit 1
    fi

    if ! mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" -e "SELECT 1" &> /dev/null; then
        echo "❌ ERROR: Could not connect to MySQL database"
        echo "   Check your configuration and network connectivity"
        exit 1
    fi

elif [[ "$DB_URL" =~ ^postgresql:// ]]; then
    echo "❌ ERROR: PostgreSQL is not yet supported by this script"
    echo "   Please run manual verification queries from UPGRADE_1.1.0.md"
    exit 1

else
    echo "❌ ERROR: Unknown database type in db_url: $DB_URL"
    exit 1
fi

echo ""

# Function to run SQL query based on DB type
run_query() {
    local query="$1"

    if [ "$DB_TYPE" = "sqlite" ]; then
        sqlite3 "$DB_FILE" "$query"
    elif [ "$DB_TYPE" = "mysql" ]; then
        mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" -e "$query"
    fi
}

run_query_silent() {
    local query="$1"

    if [ "$DB_TYPE" = "sqlite" ]; then
        sqlite3 "$DB_FILE" "$query" 2>/dev/null
    elif [ "$DB_TYPE" = "mysql" ]; then
        mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASS" "$DB_NAME" -sN -e "$query" 2>/dev/null
    fi
}

# Get metadata ID
METADATA_ID=$(run_query_silent "
SELECT id FROM statistics_meta
WHERE statistic_id LIKE '%enelgrid%consumption%'
AND statistic_id LIKE '%sensor:%'
LIMIT 1;
")

if [ -z "$METADATA_ID" ]; then
    echo "❌ ERROR: Could not find enelgrid statistics in database"
    echo "   Make sure EnelGrid integration is installed and has collected data"
    exit 1
fi

echo "✓ Found enelgrid statistics (metadata_id: $METADATA_ID)"
echo ""

# Check config entry version
echo "1. Config Entry Version"
echo "----------------------"
run_query "
SELECT
    entry_id,
    version,
    domain,
    title,
    CASE
        WHEN version = 1 THEN '⚠️  Migration NOT run yet'
        WHEN version = 2 THEN '✅ Migration completed'
        ELSE '❓ Unknown version'
    END as status
FROM config_entries
WHERE domain = 'enelgrid';
"
echo ""

# Check for anomalous jumps
echo "2. Month Boundary Jumps (should be < 1000 kWh after migration)"
echo "----------------------"
if [ "$DB_TYPE" = "sqlite" ]; then
    run_query "
SELECT
    datetime(curr.start_ts, 'unixepoch', 'localtime') as timestamp,
    ROUND(prev.sum, 2) as prev_month_last,
    ROUND(curr.sum, 2) as curr_month_first,
    ROUND(curr.sum - prev.sum, 2) as jump_kWh,
    CASE
        WHEN ABS(curr.sum - prev.sum) > 1000 THEN '❌ ANOMALOUS'
        WHEN ABS(curr.sum - prev.sum) < 100 THEN '✅ NORMAL'
        ELSE '⚠️  CHECK'
    END as status
FROM statistics curr
JOIN statistics prev ON prev.metadata_id = curr.metadata_id
WHERE curr.metadata_id = $METADATA_ID
  AND strftime('%d', datetime(curr.start_ts, 'unixepoch')) = '01'
  AND strftime('%H', datetime(curr.start_ts, 'unixepoch')) = '00'
  AND prev.start_ts = (
      SELECT MAX(start_ts)
      FROM statistics
      WHERE metadata_id = curr.metadata_id
        AND start_ts < curr.start_ts
  )
ORDER BY curr.start_ts;
" 2>/dev/null || echo "No month boundaries found in data"
else
    run_query "
SELECT
    FROM_UNIXTIME(curr.start_ts) as timestamp,
    ROUND(prev.sum, 2) as prev_month_last,
    ROUND(curr.sum, 2) as curr_month_first,
    ROUND(curr.sum - prev.sum, 2) as jump_kWh,
    CASE
        WHEN ABS(curr.sum - prev.sum) > 1000 THEN '❌ ANOMALOUS'
        WHEN ABS(curr.sum - prev.sum) < 100 THEN '✅ NORMAL'
        ELSE '⚠️  CHECK'
    END as status
FROM statistics curr
JOIN statistics prev ON prev.metadata_id = curr.metadata_id
WHERE curr.metadata_id = $METADATA_ID
  AND DAY(FROM_UNIXTIME(curr.start_ts)) = 1
  AND HOUR(FROM_UNIXTIME(curr.start_ts)) = 0
  AND prev.start_ts = (
      SELECT MAX(start_ts)
      FROM statistics
      WHERE metadata_id = curr.metadata_id
        AND start_ts < curr.start_ts
  )
ORDER BY curr.start_ts;
" 2>/dev/null || echo "No month boundaries found in data"
fi
echo ""

# Check daily consumption values
echo "3. Daily Consumption - First Days of Each Month"
echo "----------------------"
if [ "$DB_TYPE" = "sqlite" ]; then
    run_query "
SELECT
    date(start_ts, 'unixepoch') as day,
    ROUND(MIN(sum), 2) as day_start_kWh,
    ROUND(MAX(sum), 2) as day_end_kWh,
    ROUND(MAX(sum) - MIN(sum), 2) as daily_consumption_kWh,
    CASE
        WHEN (MAX(sum) - MIN(sum)) > 1000 THEN '❌ ANOMALOUS (>1000 kWh)'
        WHEN (MAX(sum) - MIN(sum)) BETWEEN 5 AND 30 THEN '✅ NORMAL (5-30 kWh)'
        ELSE '⚠️  CHECK'
    END as status
FROM statistics
WHERE metadata_id = $METADATA_ID
  AND strftime('%d', datetime(start_ts, 'unixepoch')) = '01'
  AND datetime(start_ts, 'unixepoch') >= datetime('now', '-6 months')
GROUP BY date(start_ts, 'unixepoch')
ORDER BY day;
" 2>/dev/null || echo "No data found"
else
    run_query "
SELECT
    DATE(FROM_UNIXTIME(start_ts)) as day,
    ROUND(MIN(sum), 2) as day_start_kWh,
    ROUND(MAX(sum), 2) as day_end_kWh,
    ROUND(MAX(sum) - MIN(sum), 2) as daily_consumption_kWh,
    CASE
        WHEN (MAX(sum) - MIN(sum)) > 1000 THEN '❌ ANOMALOUS (>1000 kWh)'
        WHEN (MAX(sum) - MIN(sum)) BETWEEN 5 AND 30 THEN '✅ NORMAL (5-30 kWh)'
        ELSE '⚠️  CHECK'
    END as status
FROM statistics
WHERE metadata_id = $METADATA_ID
  AND DAY(FROM_UNIXTIME(start_ts)) = 1
  AND FROM_UNIXTIME(start_ts) >= DATE_SUB(NOW(), INTERVAL 6 MONTH)
GROUP BY DATE(FROM_UNIXTIME(start_ts))
ORDER BY day;
" 2>/dev/null || echo "No data found"
fi
echo ""

# Monthly totals
echo "4. Monthly Consumption Totals"
echo "----------------------"
if [ "$DB_TYPE" = "sqlite" ]; then
    run_query "
SELECT
    strftime('%Y-%m', datetime(start_ts, 'unixepoch')) as month,
    ROUND(MIN(sum), 2) as month_start_kWh,
    ROUND(MAX(sum), 2) as month_end_kWh,
    ROUND(MAX(sum) - MIN(sum), 2) as monthly_total_kWh,
    COUNT(DISTINCT date(start_ts, 'unixepoch')) as days_with_data
FROM statistics
WHERE metadata_id = $METADATA_ID
  AND datetime(start_ts, 'unixepoch') >= datetime('now', '-6 months')
GROUP BY strftime('%Y-%m', datetime(start_ts, 'unixepoch'))
ORDER BY month;
" 2>/dev/null || echo "No data found"
else
    run_query "
SELECT
    DATE_FORMAT(FROM_UNIXTIME(start_ts), '%Y-%m') as month,
    ROUND(MIN(sum), 2) as month_start_kWh,
    ROUND(MAX(sum), 2) as month_end_kWh,
    ROUND(MAX(sum) - MIN(sum), 2) as monthly_total_kWh,
    COUNT(DISTINCT DATE(FROM_UNIXTIME(start_ts))) as days_with_data
FROM statistics
WHERE metadata_id = $METADATA_ID
  AND FROM_UNIXTIME(start_ts) >= DATE_SUB(NOW(), INTERVAL 6 MONTH)
GROUP BY DATE_FORMAT(FROM_UNIXTIME(start_ts), '%Y-%m')
ORDER BY month;
" 2>/dev/null || echo "No data found"
fi
echo ""

# Sample of raw data around month boundaries
echo "5. Sample Data: Recent Month Transition"
echo "----------------------"
# Get the most recent month transition
if [ "$DB_TYPE" = "sqlite" ]; then
    RECENT_MONTH=$(run_query_silent "
SELECT strftime('%Y-%m-01', datetime(start_ts, 'unixepoch'))
FROM statistics
WHERE metadata_id = $METADATA_ID
  AND strftime('%d', datetime(start_ts, 'unixepoch')) = '01'
ORDER BY start_ts DESC
LIMIT 1;
")
else
    RECENT_MONTH=$(run_query_silent "
SELECT DATE_FORMAT(FROM_UNIXTIME(start_ts), '%Y-%m-01')
FROM statistics
WHERE metadata_id = $METADATA_ID
  AND DAY(FROM_UNIXTIME(start_ts)) = 1
ORDER BY start_ts DESC
LIMIT 1;
")
fi

if [ -n "$RECENT_MONTH" ]; then
    PREV_MONTH=$(date -d "$RECENT_MONTH -1 day" +%Y-%m-%d 2>/dev/null || date -v-1d -j -f "%Y-%m-%d" "$RECENT_MONTH" +%Y-%m-%d 2>/dev/null || echo "")

    if [ -n "$PREV_MONTH" ]; then
        if [ "$DB_TYPE" = "sqlite" ]; then
            run_query "
SELECT
    datetime(start_ts, 'unixepoch', 'localtime') as timestamp,
    ROUND(sum, 2) as cumulative_kWh,
    ROUND(sum - LAG(sum) OVER (ORDER BY start_ts), 2) as hourly_delta_kWh
FROM statistics
WHERE metadata_id = $METADATA_ID
  AND datetime(start_ts, 'unixepoch') BETWEEN datetime('$PREV_MONTH 20:00:00') AND datetime('$RECENT_MONTH 04:00:00')
ORDER BY start_ts;
            " 2>/dev/null || echo "No transition data found"
        else
            run_query "
SELECT
    FROM_UNIXTIME(start_ts) as timestamp,
    ROUND(sum, 2) as cumulative_kWh,
    ROUND(sum - LAG(sum) OVER (ORDER BY start_ts), 2) as hourly_delta_kWh
FROM statistics
WHERE metadata_id = $METADATA_ID
  AND FROM_UNIXTIME(start_ts) BETWEEN '$PREV_MONTH 20:00:00' AND '$RECENT_MONTH 04:00:00'
ORDER BY start_ts;
            " 2>/dev/null || echo "No transition data found"
        fi
    fi
else
    echo "No month transitions found"
fi
echo ""

echo "=========================================="
echo "Summary"
echo "=========================================="

# Count anomalies
if [ "$DB_TYPE" = "sqlite" ]; then
    ANOMALY_COUNT=$(run_query_silent "
SELECT COUNT(*)
FROM (
    SELECT
        curr.start_ts,
        curr.sum - prev.sum as jump
    FROM statistics curr
    JOIN statistics prev ON prev.metadata_id = curr.metadata_id
    WHERE curr.metadata_id = $METADATA_ID
      AND strftime('%d', datetime(curr.start_ts, 'unixepoch')) = '01'
      AND strftime('%H', datetime(curr.start_ts, 'unixepoch')) = '00'
      AND prev.start_ts = (
          SELECT MAX(start_ts)
          FROM statistics
          WHERE metadata_id = curr.metadata_id
            AND start_ts < curr.start_ts
      )
      AND ABS(curr.sum - prev.sum) > 1000
);
" 2>/dev/null || echo "0")
else
    ANOMALY_COUNT=$(run_query_silent "
SELECT COUNT(*)
FROM (
    SELECT
        curr.start_ts,
        curr.sum - prev.sum as jump
    FROM statistics curr
    JOIN statistics prev ON prev.metadata_id = curr.metadata_id
    WHERE curr.metadata_id = $METADATA_ID
      AND DAY(FROM_UNIXTIME(curr.start_ts)) = 1
      AND HOUR(FROM_UNIXTIME(curr.start_ts)) = 0
      AND prev.start_ts = (
          SELECT MAX(start_ts)
          FROM statistics
          WHERE metadata_id = curr.metadata_id
            AND start_ts < curr.start_ts
      )
      AND ABS(curr.sum - prev.sum) > 1000
) anomalies;
" 2>/dev/null || echo "0")
fi

CONFIG_VERSION=$(run_query_silent "
SELECT version FROM config_entries WHERE domain = 'enelgrid' LIMIT 1;
" 2>/dev/null || echo "unknown")

echo "Config Entry Version: $CONFIG_VERSION"
echo "Anomalous Jumps Found: $ANOMALY_COUNT"
echo ""

if [ "$CONFIG_VERSION" = "1" ] && [ "$ANOMALY_COUNT" -gt "0" ]; then
    echo "Status: ⚠️  Migration NOT run yet - $ANOMALY_COUNT anomalies detected"
    echo "Action: Install v1.1.0 and restart Home Assistant to fix"
elif [ "$CONFIG_VERSION" = "2" ] && [ "$ANOMALY_COUNT" -eq "0" ]; then
    echo "Status: ✅ Migration SUCCESSFUL - All data corrected!"
    echo "Action: None - Your data is now clean"
elif [ "$CONFIG_VERSION" = "2" ] && [ "$ANOMALY_COUNT" -gt "0" ]; then
    echo "Status: ❌ Migration completed but anomalies remain"
    echo "Action: Report issue on GitHub with full output of this script"
else
    echo "Status: ✅ No anomalies detected (clean data)"
    echo "Action: None needed"
fi

echo ""
echo "=========================================="
