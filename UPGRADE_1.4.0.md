# Upgrade to EnelGrid 1.4.0 - Fix Historical Data Bug

## What This Upgrade Does

**Version 1.4.0** fixes a critical bug that caused anomalous jumps in cumulative consumption values at month boundaries. This resulted in:
- First day of each month showing extremely high consumption (~15,000 kWh instead of ~15 kWh)
- Home Assistant Energy Dashboard showing only the first day of historical months in charts
- Other days appearing "missing" because they were compressed on the scale

## What Happens When You Upgrade

1. **Automatic Migration**: When you install v1.4.0, the integration will automatically:
   - Detect anomalous jumps in your historical data (jumps > 1000 kWh between months)
   - Recalculate all cumulative values to be continuous
   - Preserve all your historical data (nothing is deleted!)
   - Fix both consumption (kWh) and cost (EUR) statistics

2. **One-Time Process**: The migration runs only once and takes ~10-30 seconds depending on data volume

3. **Immediate Effect**: After migration completes:
   - Historical months will show correct daily consumption charts
   - All days will be visible in the Energy Dashboard
   - Future data will be saved correctly (no more jumps)

## Installation Instructions for Local Testing

### Step 1: Backup Your Data (IMPORTANT!)

Before installing, backup your Home Assistant database:

```bash
# If using MySQL/MariaDB (find credentials in your HA configuration.yaml)
mysqldump -u YOUR_DB_USER -pYOUR_DB_PASSWORD YOUR_DB_NAME > /tmp/ha_backup_$(date +%Y%m%d).sql

# If using SQLite (default HA)
cp /config/home-assistant_v2.db /config/home-assistant_v2.db.backup

# Alternative: Use Home Assistant's built-in backup
# Settings → System → Backups → Create backup
```

### Step 2: Install the Updated Integration

#### Option A: Direct Copy (Recommended for Testing)

```bash
# Navigate to your Home Assistant custom_components directory
cd /config/custom_components/

# Backup current version
mv enelgrid enelgrid_v1.0.0_backup

# Copy the new version
cp -r /Users/sathia/localhost/enelgrid/custom_components/enelgrid ./
```

#### Option B: HACS (For Production Deployment)

1. Commit and push changes to GitHub
2. Create a new release tag `v1.4.0`
3. Update via HACS interface

### Step 3: Restart Home Assistant

```bash
# From HA CLI or UI
ha core restart

# Or via GUI: Settings → System → Restart
```

### Step 4: Monitor Migration Logs

The migration will start automatically when HA restarts. Monitor the logs:

```bash
# Follow logs in real-time
tail -f /config/home-assistant.log | grep enelgrid

# Or via GUI: Settings → System → Logs
```

You should see log entries like:
```
INFO Migrating enelgrid config entry from version 1
INFO Starting statistics migration for sensor:enelgrid_it001e00000000_consumption
INFO Found 5832 historical statistics records
INFO Found anomalous jump at (2025, 3): 28464.37 → 58131.12 (jump: 29666.75 kWh)
INFO Found anomalous jump at (2025, 4): 29048.62 → 84447.09 (jump: 55398.47 kWh)
... (more jumps)
INFO Processed 5832 statistics records across 9 months
INFO Created backup of original statistics at: /config/.storage/enelgrid_backup_it001e00000000_v1.json
INFO Writing corrected consumption statistics...
INFO Writing corrected cost statistics...
INFO Statistics migration completed successfully
INFO Backup saved at: /config/.storage/enelgrid_backup_it001e00000000_v1.json (can be restored if needed)
INFO Migration to version 2 complete
```

**Automatic Backup:** The migration automatically creates a backup of your original data before making any changes. The backup is saved in `/config/.storage/enelgrid_backup_<your_pod>_v1.json` and can be used to restore if something goes wrong.

### Step 5: Verify the Fix

#### Query 1: Check for Remaining Anomalous Jumps

```sql
# First, find your metadata_id:
mysql -u YOUR_DB_USER -pYOUR_DB_PASSWORD YOUR_DB_NAME -e "
SELECT id, statistic_id FROM statistics_meta
WHERE statistic_id LIKE '%enelgrid%consumption%';
"

# Then check for anomalies (replace METADATA_ID with the id from above):
mysql -u YOUR_DB_USER -pYOUR_DB_PASSWORD YOUR_DB_NAME -e "
SELECT
    DATE(FROM_UNIXTIME(start_ts)) as day,
    MIN(sum) as first_hour,
    MAX(sum) as last_hour,
    MAX(sum) - MIN(sum) as daily_consumption
FROM statistics
WHERE metadata_id = METADATA_ID
  AND FROM_UNIXTIME(start_ts) >= '2025-08-01'
  AND FROM_UNIXTIME(start_ts) < '2025-10-01'
GROUP BY DATE(FROM_UNIXTIME(start_ts))
ORDER BY day
LIMIT 10;
"
```

**Expected Result**: All `daily_consumption` values should be between 10-25 kWh (no more 15,000+ kWh!)

#### Query 2: Check Month-to-Month Continuity

```sql
mysql -u YOUR_DB_USER -pYOUR_DB_PASSWORD YOUR_DB_NAME -e "
SELECT
    FROM_UNIXTIME(start_ts) as timestamp,
    sum
FROM statistics
WHERE metadata_id = METADATA_ID
  AND (
    FROM_UNIXTIME(start_ts) BETWEEN '2025-08-31 22:00:00' AND '2025-09-01 02:00:00'
    OR FROM_UNIXTIME(start_ts) BETWEEN '2025-09-30 22:00:00' AND '2025-10-01 02:00:00'
  )
ORDER BY start_ts;
"
```

**Expected Result**: Values should be continuous (no jumps of thousands of kWh between 23:00 of last day and 00:00 of first day)

#### Query 3: Verify Config Entry Version

```sql
mysql -u YOUR_DB_USER -pYOUR_DB_PASSWORD YOUR_DB_NAME -e "
SELECT entry_id, version, domain, title
FROM config_entries
WHERE domain = 'enelgrid';
"
```

**Expected Result**: `version` should be `2`

### Step 6: Automated Verification Script

Run the verification script to confirm everything worked:

```bash
cd /path/to/enelgrid
./verify_migration.sh
```

The script will automatically:
- Detect your database type (MySQL or SQLite)
- Read credentials from configuration.yaml
- Check for anomalous jumps
- Verify config entry version
- Show daily/monthly consumption stats

Expected output:
```
Config Entry Version: 2
Anomalous Jumps Found: 0
Status: ✅ Migration SUCCESSFUL - All data corrected!
```

### Step 7: Check Energy Dashboard

1. Go to **Energy Dashboard** in Home Assistant
2. Select **September 2025** (or any historical month)
3. **Before**: You should have seen only the first day with a huge spike
4. **After**: You should see all 30 days with normal consumption (10-20 kWh/day)

## Troubleshooting

### Migration Didn't Run

**Symptom**: Logs don't show any migration messages

**Solution**:
1. Check config entry version:
   ```sql
   SELECT version FROM config_entries WHERE domain = 'enelgrid';
   ```
2. If version is already `2`, migration was skipped (already done or fresh install)
3. To force re-migration, manually set version back to 1:
   ```sql
   UPDATE config_entries SET version = 1 WHERE domain = 'enelgrid';
   ```
4. Restart Home Assistant

### Migration Failed with Errors

**Symptom**: Errors in logs during migration

**Solution**:

**Option 1: Restore from automatic backup (recommended)**

The migration creates an automatic backup before making changes. To restore:

1. Check if backup exists:
```bash
ls -l /config/.storage/enelgrid_backup_*_v1.json
```

2. Validate backup:
```bash
python3 restore_backup.py /config/.storage/enelgrid_backup_it001e00000000_v1.json --validate-only
```

3. Note: Automatic restore is not yet implemented. For now, restore from your database backup (see Option 2).

**Option 2: Restore from database backup**

```bash
# Stop Home Assistant
ha core stop

# Restore database (MySQL/MariaDB)
mysql -u YOUR_DB_USER -pYOUR_DB_PASSWORD YOUR_DB_NAME < /tmp/ha_backup_YYYYMMDD.sql

# OR restore SQLite database
cp /config/home-assistant_v2.db.backup /config/home-assistant_v2.db

# OR restore from HA built-in backup
# Settings → System → Backups → Restore

# Restart Home Assistant
ha core start
```

**Option 3: Contact support**

Open an issue at https://github.com/sathia-musso/enelgrid/issues

Include:
- The backup file from `/config/.storage/enelgrid_backup_*_v1.json`
- Full migration logs

### Data Still Shows Jumps

**Symptom**: After migration, charts still show anomalies

**Solution**:
1. Clear browser cache (Energy Dashboard caches data)
2. Wait 5-10 minutes for HA to rebuild statistics cache
3. Force refresh: Settings → System → Repair → "Rebuild statistics"

## Rollback Procedure

If you need to rollback to v1.0.0:

```bash
# Restore database backup (MySQL)
mysql -u YOUR_DB_USER -pYOUR_DB_PASSWORD YOUR_DB_NAME < /tmp/ha_backup_YYYYMMDD.sql

# OR restore SQLite
cp /config/home-assistant_v2.db.backup /config/home-assistant_v2.db

# Restore old integration code
cd /config/custom_components/
rm -rf enelgrid
mv enelgrid_v1.0.0_backup enelgrid

# Restart Home Assistant
ha core restart
```

## Technical Details

### What Changed

1. **sensor.py**: Fixed `cumulative_offset` calculation
   - **Before**: Calculated inside the day loop → caused double-counting
   - **After**: Calculated once before the loop → correct cumulative values

2. **sensor.py**: Fixed cost calculation
   - **Before**: Used relative cumulative value (without offset)
   - **After**: Uses absolute cumulative value (with offset)

3. **__init__.py**: Added automatic migration logic with backup
   - Detects jumps > 1000 kWh between months
   - Creates automatic backup before making changes
   - Recalculates all cumulative values to be continuous
   - Preserves daily delta (actual consumption)
   - Writes backup to `/config/.storage/enelgrid_backup_<pod>_v1.json`

4. **config_flow.py**: Updated VERSION from 1 to 2
   - Triggers automatic migration on first load

### Files Modified

- `custom_components/enelgrid/__init__.py` (+200 lines - migration + backup)
- `custom_components/enelgrid/sensor.py` (~20 lines - fixed cumulative_offset bug)
- `custom_components/enelgrid/config_flow.py` (VERSION: 1 → 2)
- `custom_components/enelgrid/manifest.json` (version: 1.3.0 → 1.4.0)
- `custom_components/enelgrid/const.py` (fixed CONF_POD and CONF_USER_NUMBER)

### New Files

- `verify_migration.sh` - Automated verification script with auto-detection
- `restore_backup.py` - Backup validation and restore utility

### Database Impact

- **Reads**: ~5000-10000 statistics records (all historical data)
- **Writes**: Same number of records (UPDATE operation via async_add_external_statistics)
- **Duration**: ~10-30 seconds depending on data volume
- **Data Loss**: None (only recalculates cumulative sums, preserves deltas)

## Support

For issues or questions:
- GitHub Issues: https://github.com/sathia-musso/enelgrid/issues
- Include full logs from migration process
- Include output from verification queries above
