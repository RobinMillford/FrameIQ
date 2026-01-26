#!/usr/bin/env python3
"""
Migration Script: TV Show Tracking System
==========================================
Creates database tables for episode tracking, progress monitoring, and calendar features.

Usage:
    python migrate_tv_tracking.py

Tables Created:
    - tv_show_progress: Track overall show progress
    - tv_episode_watch: Track individual episode watches
    - upcoming_episode: Calendar of upcoming episodes
"""

import sys
from datetime import datetime
from app import app, db
from models import TVShowProgress, TVEpisodeWatch, UpcomingEpisode


def print_header():
    """Print migration header"""
    print("\n" + "="*70)
    print("  TV SHOW TRACKING SYSTEM - DATABASE MIGRATION")
    print("="*70)
    print(f"Migration Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70 + "\n")


def check_table_exists(table_name):
    """Check if a table already exists in the database"""
    try:
        with app.app_context():
            result = db.session.execute(
                db.text(f"SELECT to_regclass('public.{table_name}')")
            ).scalar()
            return result is not None
    except Exception:
        # For SQLite or other databases
        try:
            result = db.session.execute(
                db.text(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
            ).fetchone()
            return result is not None
        except Exception:
            return False


def backup_reminder():
    """Remind user to backup database"""
    print("⚠️  IMPORTANT: Backup Reminder")
    print("-" * 70)
    print("Before proceeding, ensure you have backed up your database.")
    print("\nFor PostgreSQL:")
    print("  pg_dump $DATABASE_URL > backup_$(date +%Y%m%d_%H%M%S).sql")
    print("\nFor SQLite:")
    print("  cp your_database.db backup_$(date +%Y%m%d_%H%M%S).db")
    print("-" * 70)
    
    response = input("\nHave you backed up your database? (yes/no): ").lower()
    if response not in ['yes', 'y']:
        print("\n❌ Migration cancelled. Please backup your database first.")
        sys.exit(0)
    print()


def check_existing_tables():
    """Check which tables already exist"""
    print("📋 Checking Existing Tables...")
    print("-" * 70)
    
    tables = {
        'tv_show_progress': TVShowProgress,
        'tv_episode_watch': TVEpisodeWatch,
        'upcoming_episode': UpcomingEpisode
    }
    
    existing = []
    missing = []
    
    for table_name, model in tables.items():
        exists = check_table_exists(table_name)
        status = "✅ EXISTS" if exists else "❌ MISSING"
        print(f"  {table_name:25s} {status}")
        
        if exists:
            existing.append(table_name)
        else:
            missing.append(table_name)
    
    print("-" * 70)
    return existing, missing


def create_tables():
    """Create all TV tracking tables"""
    print("\n🔧 Creating Database Tables...")
    print("-" * 70)
    
    try:
        with app.app_context():
            # Create all tables defined in models
            db.create_all()
            
            # Verify creation
            print("\n✅ Table Creation Successful!")
            print("\nVerifying tables...")
            
            tables = ['tv_show_progress', 'tv_episode_watch', 'upcoming_episode']
            for table_name in tables:
                exists = check_table_exists(table_name)
                status = "✅" if exists else "❌"
                print(f"  {status} {table_name}")
            
            print("-" * 70)
            return True
            
    except Exception as e:
        print(f"\n❌ Error creating tables: {e}")
        print("-" * 70)
        return False


def test_tables():
    """Test that tables are accessible"""
    print("\n🧪 Testing Table Access...")
    print("-" * 70)
    
    try:
        with app.app_context():
            # Test TVShowProgress
            progress_count = TVShowProgress.query.count()
            print(f"  ✅ tv_show_progress accessible (count: {progress_count})")
            
            # Test TVEpisodeWatch
            episode_count = TVEpisodeWatch.query.count()
            print(f"  ✅ tv_episode_watch accessible (count: {episode_count})")
            
            # Test UpcomingEpisode
            upcoming_count = UpcomingEpisode.query.count()
            print(f"  ✅ upcoming_episode accessible (count: {upcoming_count})")
            
            print("-" * 70)
            return True
            
    except Exception as e:
        print(f"  ❌ Error accessing tables: {e}")
        print("-" * 70)
        return False


def print_summary():
    """Print migration summary"""
    print("\n" + "="*70)
    print("  MIGRATION SUMMARY")
    print("="*70)
    print("\n✅ Migration completed successfully!")
    print("\nNew Features Available:")
    print("  • Track TV show episode progress")
    print("  • Mark individual episodes as watched")
    print("  • Mark entire seasons with one click")
    print("  • View episode calendar for tracked shows")
    print("  • Monitor watching status per show")
    print("\nNew API Endpoints:")
    print("  • POST /api/tv/<show_id>/start-tracking")
    print("  • GET  /api/tv/<show_id>/progress")
    print("  • POST /api/tv/<show_id>/episode/<s>/<e>/mark-watched")
    print("  • POST /api/tv/<show_id>/season/<s>/mark-watched")
    print("  • GET  /api/tv/my-shows")
    print("  • GET  /api/tv/calendar")
    print("\nNew Pages:")
    print("  • /tv/my-shows - Dashboard of tracked shows")
    print("  • /tv/calendar - Upcoming episodes calendar")
    print("\nNext Steps:")
    print("  1. Restart your Flask application")
    print("  2. Visit /tv/my-shows to see your tracked shows")
    print("  3. Go to any TV show page and click 'Start Tracking'")
    print("  4. Mark episodes as watched to track your progress")
    print("\n" + "="*70 + "\n")


def rollback_instructions():
    """Print rollback instructions"""
    print("\n⚠️  ROLLBACK INSTRUCTIONS")
    print("-" * 70)
    print("If you need to rollback this migration, run:")
    print("\n  python -c \"from app import app, db; ")
    print("  with app.app_context(): ")
    print("    db.session.execute(db.text('DROP TABLE IF EXISTS upcoming_episode')); ")
    print("    db.session.execute(db.text('DROP TABLE IF EXISTS tv_episode_watch')); ")
    print("    db.session.execute(db.text('DROP TABLE IF EXISTS tv_show_progress')); ")
    print("    db.session.commit()\"")
    print("\nThen restore from your backup.")
    print("-" * 70 + "\n")


def main():
    """Main migration function"""
    try:
        print_header()
        
        # Step 1: Backup reminder
        backup_reminder()
        
        # Step 2: Check existing tables
        existing, missing = check_existing_tables()
        
        if not missing:
            print("\n✅ All tables already exist. No migration needed.")
            print("\nIf you want to recreate tables, please drop them first:")
            print("  python -c \"from app import app, db; with app.app_context(): db.drop_all()\"")
            return
        
        if existing:
            print(f"\n⚠️  Warning: {len(existing)} table(s) already exist.")
            print("Only missing tables will be created.")
            response = input("\nContinue? (yes/no): ").lower()
            if response not in ['yes', 'y']:
                print("\n❌ Migration cancelled.")
                sys.exit(0)
        
        # Step 3: Create tables
        if not create_tables():
            print("\n❌ Migration failed during table creation.")
            rollback_instructions()
            sys.exit(1)
        
        # Step 4: Test tables
        if not test_tables():
            print("\n⚠️  Warning: Table creation succeeded but access test failed.")
            print("Tables may need manual verification.")
        
        # Step 5: Print summary
        print_summary()
        
        # Step 6: Rollback instructions
        rollback_instructions()
        
    except KeyboardInterrupt:
        print("\n\n❌ Migration cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        rollback_instructions()
        sys.exit(1)


if __name__ == '__main__':
    main()
