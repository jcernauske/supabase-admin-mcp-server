import os
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional
from datetime import datetime
import hashlib
import uuid

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from supabase import Client, create_client
from postgrest import APIError

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Security configuration
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY")  # Optional additional security layer
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")  # production, staging, development
REQUIRE_CONFIRMATION = os.environ.get("REQUIRE_CONFIRMATION", "true").lower() == "true"

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
    
    # Log startup with security info
    logger.info(f"MCP Server starting in {ENVIRONMENT} environment")
    logger.info(f"Additional security layer: {'enabled' if ADMIN_API_KEY else 'disabled'}")
    logger.info(f"Confirmation required: {REQUIRE_CONFIRMATION}")
    
    yield {
        "supabase_client": supabase_client
    }

# Create the FastMCP server instance
mcp = FastMCP(
    "Supabase Database Admin MCP Server (Secure)",
    lifespan=lifespan,
    dependencies=["supabase", "python-dotenv"],
)

def log_operation(operation: str, details: Dict[str, Any], user_context: str = "mcp_user"):
    """
    Log operations for audit trail.
    """
    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "operation": operation,
        "details": details,
        "user_context": user_context,
        "environment": ENVIRONMENT
    }
    logger.info(f"AUDIT: {json.dumps(log_entry)}")
    return log_entry

def check_authorization(operation: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Check if operation is authorized based on environment and security settings.
    """
    if ADMIN_API_KEY:
        provided_key = context.get("admin_key") if context else None
        if provided_key != ADMIN_API_KEY:
            return {"authorized": False, "reason": "Invalid admin key"}
    
    # Production environment checks
    if ENVIRONMENT == "production":
        destructive_ops = ["apply_migration", "rollback_migration", "execute_sql_info"]
        if operation in destructive_ops and REQUIRE_CONFIRMATION:
            confirmation = context.get("confirm") if context else None
            if confirmation != "yes":
                return {
                    "authorized": False, 
                    "reason": f"Production operation requires confirmation. Add 'confirm': 'yes' to proceed with {operation}"
                }
    
    return {"authorized": True}

@mcp.tool()
def check_security_status() -> Dict[str, Any]:
    """
    Check the security status of the database and identify potential issues.
    
    Returns:
        A dictionary containing security status and recommendations.
    """
    try:
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        log_operation("check_security_status", {"action": "security_audit"})
        
        security_issues = []
        recommendations = []
        
        # Check for RLS status on public tables
        try:
            rls_check = supabase.rpc('check_rls_status').execute()
            if rls_check.data:
                tables_without_rls = [t for t in rls_check.data if not t.get('rls_enabled')]
                if tables_without_rls:
                    security_issues.append({
                        "severity": "HIGH",
                        "issue": "Tables without Row Level Security",
                        "tables": tables_without_rls,
                        "impact": "Data accessible to unauthorized users"
                    })
                    recommendations.append("Enable RLS on all public tables using enable_rls_on_table()")
        except:
            recommendations.append("Install security helper functions from security_setup.sql")
        
        # Check environment security
        if ENVIRONMENT == "production" and not ADMIN_API_KEY:
            security_issues.append({
                "severity": "MEDIUM",
                "issue": "No additional API key protection in production",
                "impact": "Service key is the only authentication layer"
            })
            recommendations.append("Set ADMIN_API_KEY environment variable for additional security")
        
        return {
            "environment": ENVIRONMENT,
            "security_issues": security_issues,
            "recommendations": recommendations,
            "rls_protection": len([i for i in security_issues if "RLS" in i.get("issue", "")]) == 0,
            "additional_auth": ADMIN_API_KEY is not None,
            "audit_logging": True,
            "overall_status": "SECURE" if len(security_issues) == 0 else "NEEDS_ATTENTION"
        }
        
    except Exception as e:
        return {"error": f"Failed to check security status: {str(e)}"}

@mcp.tool()
def enable_rls_on_table(table_name: str, admin_key: Optional[str] = None, confirm: Optional[str] = None) -> Dict[str, Any]:
    """
    Enable Row Level Security on a specific table.
    
    Args:
        table_name: Name of the table to enable RLS on
        admin_key: Admin API key if required
        confirm: Confirmation for production operations
    
    Returns:
        A dictionary containing the RLS enable result.
    """
    try:
        # Check authorization
        auth_check = check_authorization("enable_rls_on_table", {"admin_key": admin_key, "confirm": confirm})
        if not auth_check["authorized"]:
            return {"error": auth_check["reason"]}
        
        log_operation("enable_rls_on_table", {"table": table_name})
        
        enable_rls_sql = f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY;'
        
        # Create a basic policy for authenticated users
        basic_policy_sql = f'''
CREATE POLICY "authenticated_access" ON "{table_name}"
    FOR ALL 
    TO authenticated 
    USING (true);
'''
        
        return {
            "table": table_name,
            "rls_sql": enable_rls_sql,
            "basic_policy_sql": basic_policy_sql,
            "instructions": [
                "1. Copy and execute the RLS SQL in Supabase SQL editor",
                "2. Optionally create the basic policy (allows all authenticated users)",
                "3. Create more specific policies based on your needs",
                "4. Test access with different user roles"
            ],
            "warning": "Enabling RLS will block all access until policies are created"
        }
        
    except Exception as e:
        return {"error": f"Failed to enable RLS: {str(e)}"}

@mcp.tool()
def create_migration(name: str, up_sql: str, down_sql: str, admin_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Create a new database migration with enhanced security checks.
    
    Args:
        name: Name of the migration (e.g., 'add_user_table')
        up_sql: SQL to apply the migration
        down_sql: SQL to rollback the migration
        admin_key: Admin API key if required

    Returns:
        A dictionary containing the migration creation result.
    """
    try:
        # Check authorization
        auth_check = check_authorization("create_migration", {"admin_key": admin_key})
        if not auth_check["authorized"]:
            return {"error": auth_check["reason"]}
        
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        # Security analysis of SQL
        security_analysis = analyze_sql_security(up_sql, down_sql)
        
        # Create migration record with security metadata
        migration_data = {
            "name": name,
            "up_sql": up_sql,
            "down_sql": down_sql,
            "created_at": datetime.utcnow().isoformat(),
            "applied": False,
            "environment": ENVIRONMENT,
            "security_analysis": security_analysis,
            "created_by": "mcp_admin"
        }
        
        log_operation("create_migration", {
            "migration_name": name,
            "security_risk": security_analysis.get("risk_level", "unknown")
        })
        
        # Store migration in migrations table
        response = supabase.table('migrations').insert(migration_data).execute()
        
        result = {
            "success": True,
            "migration_id": response.data[0]['id'] if response.data else None,
            "message": f"Migration '{name}' created successfully",
            "security_analysis": security_analysis,
            "migration": migration_data
        }
        
        # Add warnings for high-risk operations
        if security_analysis.get("risk_level") == "HIGH":
            result["warnings"] = security_analysis.get("warnings", [])
            result["recommendations"] = [
                "Review SQL carefully before applying",
                "Test in development environment first",
                "Consider backup before applying",
                "Apply during maintenance window"
            ]
        
        return result
        
    except APIError as e:
        if "relation \"migrations\" does not exist" in str(e):
            return {
                "error": "Migrations table not found. Run setup_migrations_table() first.",
                "setup_required": True
            }
        return {"error": f"Supabase API Error: {e.message}", "details": e.details}
    except Exception as e:
        return {"error": f"Failed to create migration: {str(e)}"}

def analyze_sql_security(up_sql: str, down_sql: str) -> Dict[str, Any]:
    """
    Analyze SQL for potential security risks.
    """
    analysis = {
        "risk_level": "LOW",
        "warnings": [],
        "recommendations": []
    }
    
    up_upper = up_sql.upper()
    down_upper = down_sql.upper()
    
    # Check for high-risk operations
    high_risk_operations = ["DROP TABLE", "DROP DATABASE", "DELETE FROM", "TRUNCATE"]
    medium_risk_operations = ["ALTER TABLE", "DROP COLUMN", "DROP INDEX"]
    
    for op in high_risk_operations:
        if op in up_upper or op in down_upper:
            analysis["risk_level"] = "HIGH"
            analysis["warnings"].append(f"Contains {op} operation")
    
    for op in medium_risk_operations:
        if op in up_upper or op in down_upper:
            if analysis["risk_level"] == "LOW":
                analysis["risk_level"] = "MEDIUM"
            analysis["warnings"].append(f"Contains {op} operation")
    
    # Check for RLS considerations
    if "CREATE TABLE" in up_upper and "ROW LEVEL SECURITY" not in up_upper:
        analysis["warnings"].append("New table created without explicit RLS")
        analysis["recommendations"].append("Consider enabling RLS on new tables")
    
    return analysis

@mcp.tool()
def apply_migration(migration_id: int, admin_key: Optional[str] = None, confirm: Optional[str] = None) -> Dict[str, Any]:
    """
    Apply a specific migration to the database with security checks.
    
    Args:
        migration_id: ID of the migration to apply
        admin_key: Admin API key if required
        confirm: Confirmation for production operations

    Returns:
        A dictionary containing the migration application result.
    """
    try:
        # Check authorization
        auth_check = check_authorization("apply_migration", {"admin_key": admin_key, "confirm": confirm})
        if not auth_check["authorized"]:
            return {"error": auth_check["reason"]}
        
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        # Get migration details
        migration_response = supabase.table('migrations').select('*').eq('id', migration_id).execute()
        
        if not migration_response.data:
            return {"error": f"Migration with ID {migration_id} not found"}
        
        migration = migration_response.data[0]
        
        if migration['applied']:
            return {"error": f"Migration '{migration['name']}' has already been applied"}
        
        log_operation("apply_migration", {
            "migration_id": migration_id,
            "migration_name": migration['name'],
            "environment": ENVIRONMENT
        })
        
        # Security check before applying
        security_analysis = migration.get('security_analysis', {})
        if security_analysis.get('risk_level') == 'HIGH' and ENVIRONMENT == 'production':
            return {
                "error": "High-risk migration requires manual review in production",
                "migration_name": migration['name'],
                "security_warnings": security_analysis.get('warnings', []),
                "manual_steps": [
                    "1. Review the migration SQL carefully",
                    "2. Test in staging environment",
                    "3. Backup production database",
                    "4. Apply during maintenance window",
                    "5. Monitor for issues after application"
                ]
            }
        
        # Try to use custom function for automatic application
        try:
            result = supabase.rpc('apply_migration_by_id', {'migration_id_param': migration_id}).execute()
            
            # Update migration status
            supabase.table('migrations').update({
                'applied': True,
                'applied_at': datetime.utcnow().isoformat(),
                'applied_by': 'mcp_admin'
            }).eq('id', migration_id).execute()
            
            return {
                "success": True,
                "message": result.data if result.data else f"Migration '{migration['name']}' applied successfully",
                "applied_at": datetime.utcnow().isoformat()
            }
        except:
            # Fallback: return SQL for manual execution
            return {
                "migration_name": migration['name'],
                "sql_to_execute": migration['up_sql'],
                "message": f"Execute this SQL manually, then mark migration as applied",
                "manual_mark_sql": f"UPDATE migrations SET applied = TRUE, applied_at = NOW(), applied_by = 'manual' WHERE id = {migration_id};",
                "security_analysis": security_analysis
            }
        
    except Exception as e:
        return {"error": f"Failed to apply migration: {str(e)}"}

@mcp.tool()
def rollback_migration(migration_id: int, admin_key: Optional[str] = None, confirm: Optional[str] = None) -> Dict[str, Any]:
    """
    Rollback a specific migration with security checks.
    
    Args:
        migration_id: ID of the migration to rollback
        admin_key: Admin API key if required
        confirm: Confirmation for production operations

    Returns:
        A dictionary containing the rollback result.
    """
    try:
        # Check authorization
        auth_check = check_authorization("rollback_migration", {"admin_key": admin_key, "confirm": confirm})
        if not auth_check["authorized"]:
            return {"error": auth_check["reason"]}
        
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        # Get migration details
        migration_response = supabase.table('migrations').select('*').eq('id', migration_id).execute()
        
        if not migration_response.data:
            return {"error": f"Migration with ID {migration_id} not found"}
        
        migration = migration_response.data[0]
        
        if not migration['applied']:
            return {"error": f"Migration '{migration['name']}' has not been applied yet"}
        
        log_operation("rollback_migration", {
            "migration_id": migration_id,
            "migration_name": migration['name'],
            "environment": ENVIRONMENT
        })
        
        # Try to use custom function for automatic rollback
        try:
            result = supabase.rpc('rollback_migration_by_id', {'migration_id_param': migration_id}).execute()
            
            # Update migration status
            supabase.table('migrations').update({
                'applied': False,
                'applied_at': None,
                'rolled_back_at': datetime.utcnow().isoformat(),
                'rolled_back_by': 'mcp_admin'
            }).eq('id', migration_id).execute()
            
            return {
                "success": True,
                "message": result.data if result.data else f"Migration '{migration['name']}' rolled back successfully",
                "rolled_back_at": datetime.utcnow().isoformat()
            }
        except:
            # Fallback: return SQL for manual execution
            return {
                "migration_name": migration['name'],
                "sql_to_execute": migration['down_sql'],
                "message": f"Execute this SQL manually, then mark migration as rolled back",
                "manual_mark_sql": f"UPDATE migrations SET applied = FALSE, applied_at = NULL, rolled_back_at = NOW(), rolled_back_by = 'manual' WHERE id = {migration_id};"
            }
        
    except Exception as e:
        return {"error": f"Failed to rollback migration: {str(e)}"}

@mcp.tool()
def list_migrations() -> Dict[str, Any]:
    """
    List all migrations and their status with security metadata.

    Returns:
        A dictionary containing all migrations with security information.
    """
    try:
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        response = supabase.table('migrations').select('*').order('created_at').execute()
        
        migrations = response.data if response.data else []
        
        # Add security summary
        security_summary = {
            "total_migrations": len(migrations),
            "applied": len([m for m in migrations if m.get('applied')]),
            "pending": len([m for m in migrations if not m.get('applied')]),
            "high_risk": len([m for m in migrations if m.get('security_analysis', {}).get('risk_level') == 'HIGH']),
            "environment": ENVIRONMENT
        }
        
        log_operation("list_migrations", security_summary)
        
        return {
            "migrations": migrations,
            "security_summary": security_summary
        }
        
    except APIError as e:
        if "relation \"migrations\" does not exist" in str(e):
            return {
                "error": "Migrations table does not exist. Run setup_migrations_table() first.",
                "migrations": [],
                "security_summary": {"total_migrations": 0},
                "setup_required": True
            }
        return {"error": f"Supabase API Error: {e.message}", "details": e.details}
    except Exception as e:
        return {"error": f"Failed to list migrations: {str(e)}"}

@mcp.tool()
def setup_migrations_table() -> Dict[str, Any]:
    """
    Set up the enhanced migrations table with security features.

    Returns:
        A dictionary containing the setup result.
    """
    try:
        log_operation("setup_migrations_table", {"environment": ENVIRONMENT})
        
        create_table_sql = """
-- Enhanced migration tracking table with security features
CREATE TABLE IF NOT EXISTS migrations (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    up_sql TEXT NOT NULL,
    down_sql TEXT NOT NULL,
    applied BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    applied_at TIMESTAMPTZ,
    rolled_back_at TIMESTAMPTZ,
    environment VARCHAR(50) DEFAULT 'development',
    security_analysis JSONB,
    created_by VARCHAR(255) DEFAULT 'unknown',
    applied_by VARCHAR(255),
    rolled_back_by VARCHAR(255)
);

-- Create indexes for faster lookups
CREATE INDEX IF NOT EXISTS idx_migrations_name ON migrations(name);
CREATE INDEX IF NOT EXISTS idx_migrations_applied ON migrations(applied);
CREATE INDEX IF NOT EXISTS idx_migrations_environment ON migrations(environment);

-- Enable RLS on migrations table
ALTER TABLE migrations ENABLE ROW LEVEL SECURITY;

-- Policy for admin access
CREATE POLICY "admin_access" ON migrations
    FOR ALL 
    TO service_role 
    USING (true);
        """
        
        return {
            "message": "Enhanced migrations table setup SQL ready",
            "sql": create_table_sql,
            "instructions": [
                "1. Copy the SQL above",
                "2. Go to your Supabase SQL Editor",
                "3. Paste and execute the SQL",
                "4. Run security_setup.sql for additional functions",
                "5. Test with check_security_status()"
            ],
            "features": [
                "Enhanced audit trail with user tracking",
                "Environment separation",
                "Security analysis storage",
                "RLS enabled by default",
                "Rollback tracking"
            ]
        }
        
    except Exception as e:
        return {"error": f"Failed to setup migrations table: {str(e)}"}

# Keep existing tools but add security checks
@mcp.tool()
def backup_table(table_name: str, include_data: bool = True, admin_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Generate SQL to backup a table structure and optionally its data.
    """
    try:
        # Check authorization for sensitive operations
        auth_check = check_authorization("backup_table", {"admin_key": admin_key})
        if not auth_check["authorized"]:
            return {"error": auth_check["reason"]}
        
        supabase: Client = mcp.get_context().request_context.lifespan_context["supabase_client"]
        
        log_operation("backup_table", {"table": table_name, "include_data": include_data})
        
        backup_sql = []
        backup_sql.append(f"-- Backup for table: {table_name}")
        backup_sql.append(f"-- Generated at: {datetime.utcnow().isoformat()}")
        backup_sql.append(f"-- Environment: {ENVIRONMENT}")
        backup_sql.append("")
        
        if include_data:
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
                                values.append(f"'{v.replace(chr(39), chr(39)+chr(39))}'")
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
                    "rows_backed_up": len(response.data) if response.data else 0,
                    "security_note": "Backup contains sensitive data - store securely"
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
def execute_sql_info(sql: str, admin_key: Optional[str] = None, confirm: Optional[str] = None) -> Dict[str, Any]:
    """
    Provide enhanced information about SQL execution with security analysis.
    """
    try:
        # Check authorization
        auth_check = check_authorization("execute_sql_info", {"admin_key": admin_key, "confirm": confirm})
        if not auth_check["authorized"]:
            return {"error": auth_check["reason"]}
        
        sql_clean = sql.strip()
        sql_upper = sql_clean.upper()
        
        log_operation("execute_sql_info", {"sql_type": "analysis"})
        
        # Enhanced SQL analysis
        analysis = analyze_sql_security(sql_clean, "")
        
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
            "security_analysis": analysis,
            "environment": ENVIRONMENT,
            "instructions": [
                "1. Copy the SQL statement",
                "2. Open Supabase SQL Editor",
                "3. Review security analysis",
                "4. Paste and review the SQL",
                "5. Execute when ready"
            ],
            "warnings": analysis.get("warnings", []),
            "recommendations": analysis.get("recommendations", []),
            "additional_warning": "Always backup data before running destructive operations" if not is_safe else None
        }
        
    except Exception as e:
        return {"error": f"Failed to analyze SQL: {str(e)}"}

# Keep other existing tools unchanged but add logging
@mcp.tool()
def list_tables() -> Dict[str, Any]:
    """List all tables with security context."""
    log_operation("list_tables", {"action": "schema_inspection"})
    # ... existing implementation

@mcp.tool()
def get_schema(table_name: Optional[str] = None) -> Dict[str, Any]:
    """Get schema with security context."""
    log_operation("get_schema", {"table": table_name or "all"})
    # ... existing implementation

@mcp.tool()
def clone_table_structure(source_table: str, target_table: str, admin_key: Optional[str] = None) -> Dict[str, Any]:
    """Clone table structure with authorization check."""
    auth_check = check_authorization("clone_table_structure", {"admin_key": admin_key})
    if not auth_check["authorized"]:
        return {"error": auth_check["reason"]}
    
    log_operation("clone_table_structure", {"source": source_table, "target": target_table})
    # ... existing implementation

@mcp.tool()
def generate_seed_data(table_name: str, num_rows: int = 10, admin_key: Optional[str] = None) -> Dict[str, Any]:
    """Generate seed data with authorization check."""
    auth_check = check_authorization("generate_seed_data", {"admin_key": admin_key})
    if not auth_check["authorized"]:
        return {"error": auth_check["reason"]}
    
    log_operation("generate_seed_data", {"table": table_name, "rows": num_rows})
    # ... existing implementation

if __name__ == "__main__":
    mcp.run()
