"""
Database migration script for Week 2b: List Collaboration, Categories, and Analytics
Adds new tables and relationships for enhanced list features
"""
from app import app, db
from models import ListCollaborator, ListCategory, UserListCategory, ListAnalytics, ListView

def migrate():
    """Create new tables for Week 2b features"""
    with app.app_context():
        print("Starting Week 2b Lists migration...")
        print("=" * 60)
        
        try:
            # Create all new tables
            db.create_all()
            
            print("\n✅ Successfully created tables:")
            print("   - list_collaborator (for list collaboration)")
            print("   - list_category (predefined categories)")
            print("   - user_list_category (lists <-> categories junction)")
            print("   - list_analytics (view counts, metrics)")
            print("   - list_view (individual view tracking)")
            
            # Create some default categories
            print("\n📂 Creating default categories...")
            default_categories = [
                {
                    'name': 'Best of 2024',
                    'description': 'Top movies and shows from 2024',
                    'icon': 'fa-star',
                    'color': '#f59e0b'
                },
                {
                    'name': 'Horror',
                    'description': 'Scary movies and thrillers',
                    'icon': 'fa-ghost',
                    'color': '#ef4444'
                },
                {
                    'name': 'Sci-Fi',
                    'description': 'Science fiction and futuristic content',
                    'icon': 'fa-rocket',
                    'color': '#3b82f6'
                },
                {
                    'name': 'Comedy',
                    'description': 'Funny movies and shows',
                    'icon': 'fa-face-smile',
                    'color': '#eab308'
                },
                {
                    'name': 'Drama',
                    'description': 'Dramatic and emotional stories',
                    'icon': 'fa-masks-theater',
                    'color': '#8b5cf6'
                },
                {
                    'name': 'Action',
                    'description': 'High-octane action films',
                    'icon': 'fa-explosion',
                    'color': '#dc2626'
                },
                {
                    'name': 'Oscar Winners',
                    'description': 'Academy Award winning films',
                    'icon': 'fa-trophy',
                    'color': '#fbbf24'
                },
                {
                    'name': 'Hidden Gems',
                    'description': 'Underrated and lesser-known favorites',
                    'icon': 'fa-gem',
                    'color': '#06b6d4'
                },
                {
                    'name': 'Classics',
                    'description': 'Timeless cinema',
                    'icon': 'fa-film',
                    'color': '#6366f1'
                },
                {
                    'name': 'Recently Watched',
                    'description': 'Latest viewings',
                    'icon': 'fa-clock',
                    'color': '#10b981'
                },
                {
                    'name': 'Watchlist',
                    'description': 'To watch soon',
                    'icon': 'fa-bookmark',
                    'color': '#f97316'
                },
                {
                    'name': 'Favorites',
                    'description': 'All-time favorites',
                    'icon': 'fa-heart',
                    'color': '#ec4899'
                }
            ]
            
            for cat_data in default_categories:
                existing = ListCategory.query.filter_by(name=cat_data['name']).first()
                if not existing:
                    category = ListCategory(**cat_data)
                    db.session.add(category)
            
            db.session.commit()
            print(f"✅ Created {len(default_categories)} default categories")
            
            # Initialize analytics for existing lists
            print("\n📊 Initializing analytics for existing lists...")
            from models import UserList
            
            lists_without_analytics = UserList.query.outerjoin(ListAnalytics).filter(
                ListAnalytics.id.is_(None)
            ).all()
            
            for user_list in lists_without_analytics:
                analytics = ListAnalytics(list_id=user_list.id)
                db.session.add(analytics)
            
            db.session.commit()
            print(f"✅ Initialized analytics for {len(lists_without_analytics)} lists")
            
            print("\n" + "=" * 60)
            print("✅ Week 2b Lists migration completed successfully!")
            print("\nNew Features Available:")
            print("   📝 List Collaboration - Add editors to your lists")
            print("   🏷️  List Categories - Tag lists with themes")
            print("   📊 List Analytics - Track views and engagement")
            print("\nAPI Endpoints:")
            print("   POST   /api/lists/<id>/collaborators")
            print("   DELETE /api/lists/<id>/collaborators/<user_id>")
            print("   POST   /api/lists/<id>/categories")
            print("   GET    /api/lists/<id>/analytics")
            print("   GET    /api/categories")
            
        except Exception as e:
            print(f"\n❌ Error during migration: {e}")
            db.session.rollback()
            import traceback
            traceback.print_exc()
            raise

if __name__ == "__main__":
    migrate()
