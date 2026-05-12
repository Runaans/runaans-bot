from database import plays_col

# Delete MongoDB files with older import scripts
result = plays_col.delete_many({
    "$or": [
        {"posted_by": "imported"},
        {"source": "sheet"}
    ]
})

print(f"Deleted {result.deleted_count} old plays.")