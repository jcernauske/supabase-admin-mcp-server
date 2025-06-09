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

-- Policy for admin access (service role can bypass)
CREATE POLICY "admin_access" ON migrations
    FOR ALL 
    TO service_role 
    USING (true);

-- Optional: Policy for authenticated users to read migrations
CREATE POLICY "read_access" ON migrations
    FOR SELECT 
    TO authenticated 
    USING (true);

-- Security helper function to check RLS status
CREATE OR REPLACE FUNCTION check_rls_status()
RETURNS TABLE(
    table_name TEXT,
    table_schema TEXT,
    rls_enabled BOOLEAN,
    policy_count INTEGER
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        t.table_name::TEXT,
        t.table_schema::TEXT,
        t.row_security::BOOLEAN as rls_enabled,
        COALESCE(p.policy_count, 0)::INTEGER as policy_count
    FROM information_schema.tables t
    LEFT JOIN (
        SELECT 
            tablename,
            COUNT(*) as policy_count
        FROM pg_policies 
        GROUP BY tablename
    ) p ON t.table_name = p.tablename
    WHERE t.table_schema = 'public'
    ORDER BY t.table_name;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Helper function to get table information with security details
CREATE OR REPLACE FUNCTION get_table_security_info()
RETURNS TABLE(
    table_name TEXT,
    table_schema TEXT,
    table_type TEXT,
    column_count INTEGER,
    rls_enabled BOOLEAN,
    has_policies BOOLEAN
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        t.table_name::TEXT,
        t.table_schema::TEXT,
        t.table_type::TEXT,
        COUNT(c.column_name)::INTEGER as column_count,
        COALESCE(t.row_security, false)::BOOLEAN as rls_enabled,
        (COUNT(p.policyname) > 0)::BOOLEAN as has_policies
    FROM information_schema.tables t
    LEFT JOIN information_schema.columns c ON t.table_name = c.table_name 
        AND t.table_schema = c.table_schema
    LEFT JOIN pg_policies p ON t.table_name = p.tablename
    WHERE t.table_schema NOT IN ('information_schema', 'pg_catalog', 'auth', 'storage', 'realtime')
    GROUP BY t.table_name, t.table_schema, t.table_type, t.row_security
    ORDER BY t.table_schema, t.table_name;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to describe a table's columns with security metadata
CREATE OR REPLACE FUNCTION describe_table_secure(table_name_param TEXT)
RETURNS TABLE(
    column_name TEXT,
    data_type TEXT,
    is_nullable TEXT,
    column_default TEXT,
    is_primary_key BOOLEAN,
    has_index BOOLEAN
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        c.column_name::TEXT,
        c.data_type::TEXT,
        c.is_nullable::TEXT,
        c.column_default::TEXT,
        CASE WHEN tc.constraint_type = 'PRIMARY KEY' THEN TRUE ELSE FALSE END as is_primary_key,
        (COUNT(i.indexname) > 0)::BOOLEAN as has_index
    FROM information_schema.columns c
    LEFT JOIN information_schema.key_column_usage kcu ON c.column_name = kcu.column_name 
        AND c.table_name = kcu.table_name AND c.table_schema = kcu.table_schema
    LEFT JOIN information_schema.table_constraints tc ON kcu.constraint_name = tc.constraint_name 
        AND kcu.table_schema = tc.table_schema
    LEFT JOIN pg_indexes i ON c.table_name = i.tablename 
        AND position(c.column_name in i.indexdef) > 0
    WHERE c.table_name = table_name_param
        AND c.table_schema = 'public'
    GROUP BY c.column_name, c.data_type, c.is_nullable, c.column_default, tc.constraint_type, c.ordinal_position
    ORDER BY c.ordinal_position;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to get all schemas with security information
CREATE OR REPLACE FUNCTION get_all_schemas_secure()
RETURNS TABLE(
    table_name TEXT,
    column_name TEXT,
    data_type TEXT,
    is_nullable TEXT,
    column_default TEXT,
    rls_enabled BOOLEAN
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        c.table_name::TEXT,
        c.column_name::TEXT,
        c.data_type::TEXT,
        c.is_nullable::TEXT,
        c.column_default::TEXT,
        COALESCE(t.row_security, false)::BOOLEAN as rls_enabled
    FROM information_schema.columns c
    LEFT JOIN information_schema.tables t ON c.table_name = t.table_name 
        AND c.table_schema = t.table_schema
    WHERE c.table_schema = 'public'
    ORDER BY c.table_name, c.ordinal_position;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Enhanced function to apply a migration with audit logging
CREATE OR REPLACE FUNCTION apply_migration_by_id(migration_id_param INTEGER)
RETURNS TEXT AS $$
DECLARE
    migration_record RECORD;
    result TEXT;
    start_time TIMESTAMPTZ;
    end_time TIMESTAMPTZ;
BEGIN
    start_time := NOW();
    
    -- Get the migration
    SELECT * INTO migration_record FROM migrations WHERE id = migration_id_param;
    
    IF NOT FOUND THEN
        RETURN 'Migration not found';
    END IF;
    
    IF migration_record.applied THEN
        RETURN 'Migration already applied';
    END IF;
    
    BEGIN
        -- Execute the up_sql
        EXECUTE migration_record.up_sql;
        
        end_time := NOW();
        
        -- Mark as applied with audit information
        UPDATE migrations 
        SET 
            applied = TRUE, 
            applied_at = end_time,
            applied_by = current_user
        WHERE id = migration_id_param;
        
        -- Log the successful application
        INSERT INTO migration_audit_log (
            migration_id,
            action,
            executed_by,
            execution_time_ms,
            success,
            created_at
        ) VALUES (
            migration_id_param,
            'apply',
            current_user,
            EXTRACT(EPOCH FROM (end_time - start_time)) * 1000,
            true,
            end_time
        );
        
        RETURN 'Migration applied successfully in ' || 
               ROUND(EXTRACT(EPOCH FROM (end_time - start_time)) * 1000) || 'ms';
    EXCEPTION
        WHEN OTHERS THEN
            end_time := NOW();
            
            -- Log the failed application
            INSERT INTO migration_audit_log (
                migration_id,
                action,
                executed_by,
                execution_time_ms,
                success,
                error_message,
                created_at
            ) VALUES (
                migration_id_param,
                'apply',
                current_user,
                EXTRACT(EPOCH FROM (end_time - start_time)) * 1000,
                false,
                SQLERRM,
                end_time
            );
            
            RETURN 'Error applying migration: ' || SQLERRM;
    END;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Enhanced function to rollback a migration with audit logging
CREATE OR REPLACE FUNCTION rollback_migration_by_id(migration_id_param INTEGER)
RETURNS TEXT AS $$
DECLARE
    migration_record RECORD;
    result TEXT;
    start_time TIMESTAMPTZ;
    end_time TIMESTAMPTZ;
BEGIN
    start_time := NOW();
    
    -- Get the migration
    SELECT * INTO migration_record FROM migrations WHERE id = migration_id_param;
    
    IF NOT FOUND THEN
        RETURN 'Migration not found';
    END IF;
    
    IF NOT migration_record.applied THEN
        RETURN 'Migration not applied yet';
    END IF;
    
    BEGIN
        -- Execute the down_sql
        EXECUTE migration_record.down_sql;
        
        end_time := NOW();
        
        -- Mark as not applied
        UPDATE migrations 
        SET 
            applied = FALSE, 
            applied_at = NULL,
            rolled_back_at = end_time,
            rolled_back_by = current_user
        WHERE id = migration_id_param;
        
        -- Log the successful rollback
        INSERT INTO migration_audit_log (
            migration_id,
            action,
            executed_by,
            execution_time_ms,
            success,
            created_at
        ) VALUES (
            migration_id_param,
            'rollback',
            current_user,
            EXTRACT(EPOCH FROM (end_time - start_time)) * 1000,
            true,
            end_time
        );
        
        RETURN 'Migration rolled back successfully in ' || 
               ROUND(EXTRACT(EPOCH FROM (end_time - start_time)) * 1000) || 'ms';
    EXCEPTION
        WHEN OTHERS THEN
            end_time := NOW();
            
            -- Log the failed rollback
            INSERT INTO migration_audit_log (
                migration_id,
                action,
                executed_by,
                execution_time_ms,
                success,
                error_message,
                created_at
            ) VALUES (
                migration_id_param,
                'rollback',
                current_user,
                EXTRACT(EPOCH FROM (end_time - start_time)) * 1000,
                false,
                SQLERRM,
                end_time
            );
            
            RETURN 'Error rolling back migration: ' || SQLERRM;
    END;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Create audit log table for migration operations
CREATE TABLE IF NOT EXISTS migration_audit_log (
    id SERIAL PRIMARY KEY,
    migration_id INTEGER REFERENCES migrations(id),
    action VARCHAR(20) NOT NULL, -- 'apply', 'rollback'
    executed_by VARCHAR(255) NOT NULL,
    execution_time_ms NUMERIC,
    success BOOLEAN NOT NULL,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable RLS on audit log
ALTER TABLE migration_audit_log ENABLE ROW LEVEL SECURITY;

-- Policy for admin access to audit logs
CREATE POLICY "admin_audit_access" ON migration_audit_log
    FOR ALL 
    TO service_role 
    USING (true);

-- Function to get audit trail for a migration
CREATE OR REPLACE FUNCTION get_migration_audit(migration_id_param INTEGER DEFAULT NULL)
RETURNS TABLE(
    migration_id INTEGER,
    migration_name TEXT,
    action TEXT,
    executed_by TEXT,
    execution_time_ms NUMERIC,
    success BOOLEAN,
    error_message TEXT,
    created_at TIMESTAMPTZ
) AS $$
BEGIN
    IF migration_id_param IS NOT NULL THEN
        RETURN QUERY
        SELECT 
            mal.migration_id,
            m.name::TEXT as migration_name,
            mal.action::TEXT,
            mal.executed_by::TEXT,
            mal.execution_time_ms,
            mal.success,
            mal.error_message::TEXT,
            mal.created_at
        FROM migration_audit_log mal
        JOIN migrations m ON mal.migration_id = m.id
        WHERE mal.migration_id = migration_id_param
        ORDER BY mal.created_at DESC;
    ELSE
        RETURN QUERY
        SELECT 
            mal.migration_id,
            m.name::TEXT as migration_name,
            mal.action::TEXT,
            mal.executed_by::TEXT,
            mal.execution_time_ms,
            mal.success,
            mal.error_message::TEXT,
            mal.created_at
        FROM migration_audit_log mal
        JOIN migrations m ON mal.migration_id = m.id
        ORDER BY mal.created_at DESC
        LIMIT 100;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Grant execute permissions to authenticated users
GRANT EXECUTE ON FUNCTION check_rls_status() TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION get_table_security_info() TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION describe_table_secure(TEXT) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION get_all_schemas_secure() TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION apply_migration_by_id(INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION rollback_migration_by_id(INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION get_migration_audit(INTEGER) TO authenticated, service_role;

-- Grant table permissions
GRANT ALL ON TABLE migrations TO service_role;
GRANT SELECT ON TABLE migrations TO authenticated;
GRANT ALL ON TABLE migration_audit_log TO service_role;
GRANT SELECT ON TABLE migration_audit_log TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE migrations_id_seq TO service_role;
GRANT USAGE, SELECT ON SEQUENCE migration_audit_log_id_seq TO service_role;

-- Create a view for security dashboard
CREATE OR REPLACE VIEW security_dashboard AS
SELECT 
    t.table_name,
    t.rls_enabled,
    t.has_policies,
    t.column_count,
    CASE 
        WHEN NOT t.rls_enabled THEN 'HIGH'
        WHEN t.rls_enabled AND NOT t.has_policies THEN 'MEDIUM'
        ELSE 'LOW'
    END as security_risk
FROM (
    SELECT 
        table_name,
        rls_enabled,
        has_policies,
        column_count
    FROM get_table_security_info()
    WHERE table_schema = 'public'
) t;

-- Grant access to security dashboard
GRANT SELECT ON security_dashboard TO authenticated, service_role;

COMMIT;
