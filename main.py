import os
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional
from datetime import datetime

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from supabase import Client, create_client
from postgrest import APIError

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Define the lifespan context for the MCP server
@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """
    Manage the lifecycle of the MCP server, initializing the Supabase client on startup.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")

    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in environment variables.")

    supabase_client: Client = create_client(url, key)
    
    yield {
        "supabase_client": supabase_client
    }

# Create the FastMCP server instance
mcp = FastMCP(
    "Supabase Database Admin MCP Server",
    lifespan=lifespan,
    dependencies=["supabase", "python-dotenv"],
)

@mcp.tool()
def list_tables() -> Dict[str, Any]:
    """
    List all tables in the database with their schema information.
    
    Returns:
        A dictionary containing all tables and their metadata.
    """
    try:
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        # Try to get table info using custom function
        try:
            response = supabase.rpc('get_table_info').execute()
            if response.data:
                return {"tables": response.data}
        except:
            pass
        
        # Fallback message
        return {
            "message": "Install the custom functions from migrations_setup.sql for full table listing functionality",
            "note": "You can still use other migration tools"
        }
        
    except Exception as e:
        return {"error": f"Failed to list tables: {str(e)}"}

@mcp.tool()
def get_schema(table_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Get database schema information for a specific table or all tables.
    
    Args:
        table_name: Optional table name to get schema for. If None, returns all tables.

    Returns:
        A dictionary containing schema information.
    """
    try:
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        if table_name:
            # Get specific table schema
            try:
                response = supabase.rpc('describe_table', {'table_name_param': table_name}).execute()
                if response.data:
                    return {"table": table_name, "schema": response.data}
            except:
                pass
        else:
            # Get all table schemas
            try:
                response = supabase.rpc('get_all_schemas').execute()
                if response.data:
                    return {"schema": response.data}
            except:
                pass
        
        return {
            "message": "Install the custom functions from migrations_setup.sql for schema information",
            "table": table_name if table_name else "all tables"
        }
            
    except Exception as e:
        return {"error": f"Failed to get schema: {str(e)}"}

@mcp.tool()
def create_migration(name: str, up_sql: str, down_sql: str) -> Dict[str, Any]:
    """
    Create a new database migration.
    
    Args:
        name: Name of the migration (e.g., 'add_user_table')
        up_sql: SQL to apply the migration
        down_sql: SQL to rollback the migration

    Returns:
        A dictionary containing the migration creation result.
    """
    try:
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        # Create migration record
        migration_data = {
            "name": name,
            "up_sql": up_sql,
            "down_sql": down_sql,
            "created_at": datetime.utcnow().isoformat(),
            "applied": False
        }
        
        # Store migration in migrations table
        response = supabase.table('migrations').insert(migration_data).execute()
        
        return {
            "success": True,
            "migration_id": response.data[0]['id'] if response.data else None,
            "message": f"Migration '{name}' created successfully",
            "migration": migration_data
        }
        
    except APIError as e:
        if "relation \"migrations\" does not exist" in str(e):
            return {
                "error": "Migrations table not found. Run setup_migrations_table() first.",
                "setup_required": True
            }
        return {"error": f"Supabase API Error: {e.message}", "details": e.details}
    except Exception as e:
        return {"error": f"Failed to create migration: {str(e)}"}

@mcp.tool()
def apply_migration(migration_id: int) -> Dict[str, Any]:
    """
    Apply a specific migration to the database.
    
    Args:
        migration_id: ID of the migration to apply

    Returns:
        A dictionary containing the migration application result.
    """
    try:
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        # Get migration details
        migration_response = supabase.table('migrations').select('*').eq('id', migration_id).execute()
        
        if not migration_response.data:
            return {"error": f"Migration with ID {migration_id} not found"}
        
        migration = migration_response.data[0]
        
        if migration['applied']:
            return {"error": f"Migration '{migration['name']}' has already been applied"}
        
        # Try to use custom function for automatic application
        try:
            result = supabase.rpc('apply_migration_by_id', {'migration_id_param': migration_id}).execute()
            return {
                "success": True,
                "message": result.data if result.data else f"Migration '{migration['name']}' applied successfully"
            }
        except:
            # Fallback: return SQL for manual execution
            return {
                "migration_name": migration['name'],
                "sql_to_execute": migration['up_sql'],
                "message": f"Execute this SQL manually, then mark migration as applied",
                "manual_mark_sql": f"UPDATE migrations SET applied = TRUE, applied_at = NOW() WHERE id = {migration_id};"
            }
        
    except Exception as e:
        return {"error": f"Failed to apply migration: {str(e)}"}

@mcp.tool()
def rollback_migration(migration_id: int) -> Dict[str, Any]:
    """
    Rollback a specific migration.
    
    Args:
        migration_id: ID of the migration to rollback

    Returns:
        A dictionary containing the rollback result.
    """
    try:
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        # Get migration details
        migration_response = supabase.table('migrations').select('*').eq('id', migration_id).execute()
        
        if not migration_response.data:
            return {"error": f"Migration with ID {migration_id} not found"}
        
        migration = migration_response.data[0]
        
        if not migration['applied']:
            return {"error": f"Migration '{migration['name']}' has not been applied yet"}
        
        # Try to use custom function for automatic rollback
        try:
            result = supabase.rpc('rollback_migration_by_id', {'migration_id_param': migration_id}).execute()
            return {
                "success": True,
                "message": result.data if result.data else f"Migration '{migration['name']}' rolled back successfully"
            }
        except:
            # Fallback: return SQL for manual execution
            return {
                "migration_name": migration['name'],
                "sql_to_execute": migration['down_sql'],
                "message": f"Execute this SQL manually, then mark migration as rolled back",
                "manual_mark_sql": f"UPDATE migrations SET applied = FALSE, applied_at = NULL WHERE id = {migration_id};"
            }
        
    except Exception as e:
        return {"error": f"Failed to rollback migration: {str(e)}"}

@mcp.tool()
def list_migrations() -> Dict[str, Any]:
    """
    List all migrations and their status.

    Returns:
        A dictionary containing all migrations.
    """
    try:
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        response = supabase.table('migrations').select('*').order('created_at').execute()
        
        return {
            "migrations": response.data,
            "total": len(response.data) if response.data else 0,
            "applied": len([m for m in response.data if m['applied']]) if response.data else 0,
            "pending": len([m for m in response.data if not m['applied']]) if response.data else 0
        }
        
    except APIError as e:
        if "relation \"migrations\" does not exist" in str(e):
            return {
                "error": "Migrations table does not exist. Run setup_migrations_table() first.",
                "migrations": [],
                "total": 0,
                "setup_required": True
            }
        return {"error": f"Supabase API Error: {e.message}", "details": e.details}
    except Exception as e:
        return {"error": f"Failed to list migrations: {str(e)}"}

@mcp.tool()
def setup_migrations_table() -> Dict[str, Any]:
    """
    Set up the migrations table for tracking database migrations.

    Returns:
        A dictionary containing the setup result.
    """
    try:
        create_table_sql = """
-- Migration tracking table setup
CREATE TABLE IF NOT EXISTS migrations (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    up_sql TEXT NOT NULL,
    down_sql TEXT NOT NULL,
    applied BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    applied_at TIMESTAMPTZ
);

-- Create indexes for faster lookups
CREATE INDEX IF NOT EXISTS idx_migrations_name ON migrations(name);
CREATE INDEX IF NOT EXISTS idx_migrations_applied ON migrations(applied);
        """
        
        return {
            "message": "Migrations table setup SQL ready",
            "sql": create_table_sql,
            "instructions": [
                "1. Copy the SQL above",
                "2. Go to your Supabase SQL Editor",
                "3. Paste and execute the SQL",
                "4. Optionally run the full migrations_setup.sql for additional functions"
            ]
        }
        
    except Exception as e:
        return {"error": f"Failed to setup migrations table: {str(e)}"}

@mcp.tool()
def backup_table(table_name: str, include_data: bool = True) -> Dict[str, Any]:
    """
    Generate SQL to backup a table structure and optionally its data.
    
    Args:
        table_name: Name of the table to backup
        include_data: Whether to include table data in the backup

    Returns:
        A dictionary containing the backup SQL.
    """
    try:
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        backup_sql = []
        backup_sql.append(f"-- Backup for table: {table_name}")
        backup_sql.append(f"-- Generated at: {datetime.utcnow().isoformat()}")
        backup_sql.append("")
        
        if include_data:
            # Get table data
            try:
                response = supabase.table(table_name).select('*').execute()
                
                if response.data:
                    backup_sql.append(f"-- Data for table: {table_name}")
                    backup_sql.append(f"-- {len(response.data)} rows")
                    backup_sql.append("")
                    
                    for row in response.data:
                        columns = ', '.join([f'"{k}"' for k in row.keys()])
                        values = []
                        for v in row.values():
                            if v is None:
                                values.append('NULL')
                            elif isinstance(v, str):
                                values.append(f"'{v.replace(chr(39), chr(39)+chr(39))}'")  # Escape single quotes
                            elif isinstance(v, bool):
                                values.append('TRUE' if v else 'FALSE')
                            else:
                                values.append(str(v))
                        values_str = ', '.join(values)
                        backup_sql.append(f'INSERT INTO "{table_name}" ({columns}) VALUES ({values_str});')
                
                return {
                    "table": table_name,
                    "backup_sql": "\n".join(backup_sql),
                    "include_data": include_data,
                    "rows_backed_up": len(response.data) if response.data else 0
                }
            except Exception as e:
                return {"error": f"Failed to backup table data: {str(e)}"}
        else:
            backup_sql.append(f"-- Structure only backup for: {table_name}")
            backup_sql.append("-- Use pg_dump or Supabase backup tools for structure")
            
            return {
                "table": table_name,
                "backup_sql": "\n".join(backup_sql),
                "include_data": False,
                "note": "For structure backup, use Supabase backup tools or pg_dump"
            }
        
    except Exception as e:
        return {"error": f"Failed to backup table {table_name}: {str(e)}"}

@mcp.tool()
def clone_table_structure(source_table: str, target_table: str) -> Dict[str, Any]:
    """
    Generate SQL to clone table structure from source to target.
    
    Args:
        source_table: Name of the source table
        target_table: Name of the target table

    Returns:
        A dictionary containing the clone SQL.
    """
    try:
        clone_sql = f"""
-- Clone table structure from {source_table} to {target_table}
CREATE TABLE "{target_table}" (
    LIKE "{source_table}" INCLUDING ALL
);

-- Note: This copies structure, constraints, and indexes
-- Run this SQL in your Supabase SQL editor
        """
        
        return {
            "source_table": source_table,
            "target_table": target_table,
            "clone_sql": clone_sql.strip(),
            "instructions": [
                "1. Copy the SQL above",
                "2. Execute in Supabase SQL editor",
                "3. The new table will have the same structure as the source"
            ]
        }
        
    except Exception as e:
        return {"error": f"Failed to generate clone SQL: {str(e)}"}

@mcp.tool()
def generate_seed_data(table_name: str, num_rows: int = 10) -> Dict[str, Any]:
    """
    Generate sample/seed data for a table based on its structure.
    
    Args:
        table_name: Name of the table to generate seed data for
        num_rows: Number of rows to generate

    Returns:
        A dictionary containing the seed data SQL.
    """
    try:
        # Basic seed data template
        seed_sql = f"""
-- Seed data for {table_name}
-- Generated {num_rows} sample rows
-- Customize column names and values based on your table structure

-- Example INSERT statements (customize as needed):
INSERT INTO "{table_name}" (column1, column2, column3) VALUES 
    ('sample_value_1', 'sample_value_2', NOW()),
    ('sample_value_3', 'sample_value_4', NOW());

-- For UUIDs, use: gen_random_uuid()
-- For timestamps, use: NOW()
-- For booleans, use: TRUE or FALSE
-- For numbers, use: 123, 45.67, etc.
        """
        
        return {
            "table": table_name,
            "rows": num_rows,
            "seed_sql": seed_sql.strip(),
            "instructions": [
                "1. Customize the column names to match your table",
                "2. Update the sample values with appropriate data types",
                "3. Execute in Supabase SQL editor"
            ]
        }
        
    except Exception as e:
        return {"error": f"Failed to generate seed data: {str(e)}"}

@mcp.tool()
def execute_sql_info(sql: str) -> Dict[str, Any]:
    """
    Provide information about SQL execution (does not actually execute).
    
    Args:
        sql: SQL statement to analyze

    Returns:
        A dictionary containing SQL analysis and execution guidance.
    """
    try:
        sql_clean = sql.strip()
        sql_upper = sql_clean.upper()
        
        # Analyze SQL type
        sql_type = "UNKNOWN"
        is_safe = False
        
        if sql_upper.startswith('SELECT'):
            sql_type = "SELECT (Read-only)"
            is_safe = True
        elif sql_upper.startswith('INSERT'):
            sql_type = "INSERT (Modifies data)"
        elif sql_upper.startswith('UPDATE'):
            sql_type = "UPDATE (Modifies data)"
        elif sql_upper.startswith('DELETE'):
            sql_type = "DELETE (Removes data)"
        elif sql_upper.startswith('CREATE'):
            sql_type = "CREATE (Schema change)"
        elif sql_upper.startswith('ALTER'):
            sql_type = "ALTER (Schema change)"
        elif sql_upper.startswith('DROP'):
            sql_type = "DROP (Destructive operation)"
        
        return {
            "sql": sql_clean,
            "type": sql_type,
            "is_safe": is_safe,
            "instructions": [
                "1. Copy the SQL statement",
                "2. Open Supabase SQL Editor",
                "3. Paste and review the SQL",
                "4. Execute when ready"
            ],
            "warning": "Always backup data before running destructive operations" if not is_safe else None
        }
        
    except Exception as e:
        return {"error": f"Failed to analyze SQL: {str(e)}"}

if __name__ == "__main__":
    mcp.run()
