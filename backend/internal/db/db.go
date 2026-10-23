package db

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	_ "github.com/lib/pq"
)

// DB 封装连接与迁移状态。
type DB struct {
	*sql.DB
	migrationDir string
}

func Open(databaseURL string) (*DB, error) {
	pg, err := sql.Open("postgres", databaseURL)
	if err != nil {
		return nil, fmt.Errorf("open postgres: %w", err)
	}
	pg.SetMaxOpenConns(25)
	pg.SetMaxIdleConns(5)
	pg.SetConnMaxLifetime(5 * time.Minute)

	if err := pg.Ping(); err != nil {
		return nil, fmt.Errorf("ping postgres: %w", err)
	}

	return &DB{DB: pg, migrationDir: "migrations"}, nil
}

func (d *DB) SetMigrationDir(dir string) {
	abs, err := filepath.Abs(dir)
	if err == nil {
		d.migrationDir = abs
	} else {
		d.migrationDir = dir
	}
}

func (d *DB) MigrationDir() string {
	return d.migrationDir
}

// MigrationRecord 记录单条迁移。
type MigrationRecord struct {
	Version string
	Applied bool
}

// ensureSchemaMigrations 创建迁移元数据表。
func (d *DB) ensureSchemaMigrations() error {
	_, err := d.Exec(`
		CREATE TABLE IF NOT EXISTS schema_migrations (
			version TEXT PRIMARY KEY,
			applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
		)
	`)
	return err
}

// AppliedMigrations 返回已应用的版本号集合。
func (d *DB) AppliedMigrations() (map[string]struct{}, error) {
	if err := d.ensureSchemaMigrations(); err != nil {
		return nil, err
	}
	rows, err := d.Query("SELECT version FROM schema_migrations")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	applied := make(map[string]struct{})
	for rows.Next() {
		var v string
		if err := rows.Scan(&v); err != nil {
			return nil, err
		}
		applied[v] = struct{}{}
	}
	return applied, rows.Err()
}

// MigrateUp 执行所有未应用的 .up.sql 迁移。
func (d *DB) MigrateUp() error {
	applied, err := d.AppliedMigrations()
	if err != nil {
		return err
	}

	files, err := os.ReadDir(d.migrationDir)
	if err != nil {
		return fmt.Errorf("read migration dir %s: %w", d.migrationDir, err)
	}

	var ups []string
	for _, f := range files {
		name := f.Name()
		if strings.HasSuffix(name, ".up.sql") {
			ups = append(ups, name)
		}
	}
	sort.Strings(ups)

	tx, err := d.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	for _, name := range ups {
		version := strings.TrimSuffix(name, ".up.sql")
		if _, ok := applied[version]; ok {
			continue
		}

		data, err := os.ReadFile(filepath.Join(d.migrationDir, name))
		if err != nil {
			return fmt.Errorf("read migration %s: %w", name, err)
		}

		if _, err := tx.Exec(string(data)); err != nil {
			return fmt.Errorf("apply migration %s: %w", name, err)
		}
		if _, err := tx.Exec("INSERT INTO schema_migrations (version) VALUES ($1)", version); err != nil {
			return fmt.Errorf("record migration %s: %w", name, err)
		}
	}

	return tx.Commit()
}

// MigrateDown 回滚最近一次 up 迁移（仅用于测试/开发）。
func (d *DB) MigrateDown() error {
	applied, err := d.AppliedMigrations()
	if err != nil {
		return err
	}

	// 找到最大已应用版本
	var latest string
	for v := range applied {
		if v > latest {
			latest = v
		}
	}
	if latest == "" {
		return nil
	}

	name := latest + ".down.sql"
	data, err := os.ReadFile(filepath.Join(d.migrationDir, name))
	if err != nil {
		return fmt.Errorf("read down migration %s: %w", name, err)
	}

	tx, err := d.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	if _, err := tx.Exec(string(data)); err != nil {
		return fmt.Errorf("apply down migration %s: %w", name, err)
	}
	if _, err := tx.Exec("DELETE FROM schema_migrations WHERE version = $1", latest); err != nil {
		return fmt.Errorf("remove migration record %s: %w", latest, err)
	}

	return tx.Commit()
}
