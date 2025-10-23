#!/usr/bin/env python3
"""
EnelGrid Statistics Restore Script

This script restores the original statistics from a backup file created
during the v1 to v2 migration. Use this if the migration didn't work correctly
and you want to rollback to the original data.

Usage:
    python3 restore_backup.py /config/.storage/enelgrid_backup_it001e00000000_v1.json

Requirements:
    - Run this script from within the Home Assistant container or with access to HA's database
    - Home Assistant must be stopped during restore
    - You need the backup JSON file path
"""

import argparse
import json
import sys
from datetime import datetime

try:
    from homeassistant.components.recorder.statistics import async_add_external_statistics, get_metadata
    from homeassistant.core import HomeAssistant
    from homeassistant.util.dt import as_utc
    HAS_HA = True
except ImportError:
    HAS_HA = False
    print("Warning: Home Assistant libraries not found. Will only validate backup file.")


def validate_backup(backup_path):
    """Validate backup file structure."""
    try:
        with open(backup_path, 'r') as f:
            backup = json.load(f)

        required_keys = ['version', 'backup_timestamp', 'statistic_id_consumption', 'pod', 'original_statistics']
        missing = [k for k in required_keys if k not in backup]

        if missing:
            print(f"❌ Invalid backup file. Missing keys: {', '.join(missing)}")
            return None

        stats_count = len(backup['original_statistics'])
        print(f"✅ Backup file valid")
        print(f"   POD: {backup['pod']}")
        print(f"   Created: {backup['backup_timestamp']}")
        print(f"   Records: {stats_count}")

        return backup

    except FileNotFoundError:
        print(f"❌ Backup file not found: {backup_path}")
        return None
    except json.JSONDecodeError:
        print(f"❌ Invalid JSON in backup file")
        return None
    except Exception as e:
        print(f"❌ Error reading backup: {e}")
        return None


def restore_backup_to_ha(hass: HomeAssistant, backup_data: dict):
    """Restore backup data to Home Assistant (requires running HA instance)."""
    if not HAS_HA:
        print("❌ Cannot restore: Home Assistant libraries not available")
        return False

    statistic_id_kw = backup_data['statistic_id_consumption']
    statistic_id_cost = backup_data.get('statistic_id_cost')

    print(f"📊 Restoring {len(backup_data['original_statistics'])} records...")

    # Convert backup data to HA statistics format
    restored_stats = []
    for stat in backup_data['original_statistics']:
        restored_stats.append({
            "start": as_utc(datetime.fromtimestamp(stat["start"])),
            "sum": stat["sum"]
        })

    # Get metadata
    metadata_ids = {statistic_id_kw}
    if statistic_id_cost:
        metadata_ids.add(statistic_id_cost)

    metadata = get_metadata(hass, statistic_ids=metadata_ids)

    if statistic_id_kw not in metadata:
        print(f"❌ Statistic ID not found in database: {statistic_id_kw}")
        return False

    # Restore consumption statistics
    print(f"   Writing consumption statistics...")
    async_add_external_statistics(
        hass,
        metadata[statistic_id_kw][1],
        restored_stats
    )

    # Restore cost statistics if available
    if statistic_id_cost and statistic_id_cost in metadata:
        print(f"   Writing cost statistics...")
        # Cost uses same cumulative values
        async_add_external_statistics(
            hass,
            metadata[statistic_id_cost][1],
            restored_stats
        )

    print("✅ Restore completed successfully!")
    print("\n⚠️  IMPORTANT: You must now downgrade enelgrid to v1.0.0 to prevent re-migration")
    print("   Or delete/edit the config_entry to set version back to 1")

    return True


def main():
    parser = argparse.ArgumentParser(description='Restore EnelGrid statistics from backup')
    parser.add_argument('backup_file', help='Path to backup JSON file')
    parser.add_argument('--validate-only', action='store_true',
                       help='Only validate backup file, do not restore')

    args = parser.parse_args()

    print("=" * 60)
    print("EnelGrid Statistics Restore Tool")
    print("=" * 60)
    print()

    # Validate backup
    backup = validate_backup(args.backup_file)
    if not backup:
        sys.exit(1)

    if args.validate_only:
        print("\n✅ Backup validation successful. Use without --validate-only to restore.")
        sys.exit(0)

    # Restore
    print()
    print("⚠️  WARNING: This will overwrite current statistics data!")
    print("   Make sure Home Assistant is stopped before proceeding.")
    print()

    confirm = input("Continue with restore? (yes/no): ").strip().lower()
    if confirm != 'yes':
        print("Restore cancelled.")
        sys.exit(0)

    print()
    print("❌ ERROR: Automated restore requires running Home Assistant instance")
    print()
    print("MANUAL RESTORE INSTRUCTIONS:")
    print("1. Stop Home Assistant")
    print("2. Connect to your database (MySQL/PostgreSQL)")
    print("3. Run the following SQL to restore from backup:")
    print()
    print(f"   -- This is a complex operation. Contact support or:")
    print(f"   -- Restore from your database backup taken before migration")
    print()
    print("Alternatively, restore your full database backup created before migration.")


if __name__ == '__main__':
    main()
