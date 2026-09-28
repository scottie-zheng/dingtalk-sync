-- ============================================
-- 钉钉数据同步 - 数据库 Schema
-- ============================================

-- ===== 知识库表 =====

CREATE TABLE IF NOT EXISTS ods_wiki_workspace (
    workspace_id    VARCHAR(64) PRIMARY KEY,
    workspace_name  VARCHAR(256) NOT NULL,
    description     TEXT,
    creator_id      VARCHAR(64),
    create_time     DATETIME,
    modified_time   DATETIME,
    root_node_id    VARCHAR(64),
    workspace_type  VARCHAR(32),
    url             VARCHAR(512),
    team_id         VARCHAR(64),
    status          VARCHAR(32),
    sync_time       DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ods_wiki_node (
    node_id             VARCHAR(64) PRIMARY KEY,
    workspace_id        VARCHAR(64) NOT NULL,
    parent_node_id      VARCHAR(64),
    name                VARCHAR(512) NOT NULL,
    node_type           VARCHAR(32),
    category            VARCHAR(32),
    creator_id          VARCHAR(64),
    create_time         DATETIME,
    create_timestamp    BIGINT,
    modifier_id         VARCHAR(64),
    modified_time       DATETIME,
    modified_timestamp  BIGINT,
    has_children        TINYINT(1),
    file_size           BIGINT,
    extension           VARCHAR(16),
    url                 VARCHAR(512),
    sync_time           DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (workspace_id) REFERENCES ods_wiki_workspace(workspace_id),
    INDEX idx_wiki_node_workspace (workspace_id),
    INDEX idx_wiki_node_modified (modified_time),
    INDEX idx_wiki_node_parent (parent_node_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ===== 组织部门表 =====

CREATE TABLE IF NOT EXISTS ods_org_department (
    dept_id BIGINT NOT NULL,
    dept_name VARCHAR(256),
    parent_id BIGINT,
    dept_group TINYINT(1) DEFAULT 0 COMMENT '是否为部门群',
    auto_add_user TINYINT(1) DEFAULT 0 COMMENT '自动加入人员',
    auto_approve_apply TINYINT(1) DEFAULT 0,
    from_union_org TINYINT(1) DEFAULT 0,
    resource_permission TINYINT(1) DEFAULT 0,
    sync_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dept_id),
    INDEX idx_parent (parent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ===== 通讯录用户表 =====

CREATE TABLE IF NOT EXISTS ods_contact_user (
    userid VARCHAR(64) NOT NULL,
    unionid VARCHAR(64),
    name VARCHAR(128),
    avatar TEXT,
    mobile VARCHAR(32),
    hide_mobile TINYINT(1) DEFAULT 0,
    telephone VARCHAR(32),
    job_number VARCHAR(64),
    title VARCHAR(128),
    email VARCHAR(256),
    org_email VARCHAR(256),
    work_place VARCHAR(256),
    active TINYINT(1) DEFAULT 1,
    admin TINYINT(1) DEFAULT 0,
    boss TINYINT(1) DEFAULT 0,
    leader TINYINT(1) DEFAULT 0,
    state_code VARCHAR(16),
    manager_userid VARCHAR(64),
    dept_id_list TEXT COMMENT '所属部门ID列表(JSON)',
    sync_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (userid),
    INDEX idx_unionid (unionid),
    INDEX idx_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ===== 用户-部门关联表 =====

CREATE TABLE IF NOT EXISTS ods_contact_user_dept (
    id BIGINT AUTO_INCREMENT,
    userid VARCHAR(64) NOT NULL,
    dept_id BIGINT NOT NULL,
    sync_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_dept (userid, dept_id),
    INDEX idx_dept (dept_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- 云文件修改记录相关表
-- 追加到 schema/init.sql

-- 云盘空间表
CREATE TABLE IF NOT EXISTS ods_file_space (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    workspace_id    VARCHAR(128) NOT NULL COMMENT '空间ID',
    name            VARCHAR(256) COMMENT '空间名称',
    url             VARCHAR(1024) COMMENT '空间URL',
    sync_time       DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '同步时间',
    UNIQUE KEY uk_workspace_id (workspace_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='云盘空间';

-- 云文件操作日志表（修改记录）
CREATE TABLE IF NOT EXISTS ods_file_modification (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    log_id          VARCHAR(128) NOT NULL COMMENT '操作日志ID',
    action          VARCHAR(64) COMMENT '操作类型（如 edit/create/delete/move/rename/share 等）',
    operate_time    BIGINT COMMENT '操作时间（毫秒时间戳）',
    operate_time_dt DATETIME COMMENT '操作时间（格式化）',
    operator_id     VARCHAR(128) COMMENT '操作者ID',
    operator_name   VARCHAR(128) COMMENT '操作者姓名',
    scene           VARCHAR(64) COMMENT '场景（如 workspace/group 等）',
    subject_id      VARCHAR(256) COMMENT '文件/对象ID',
    subject_name    VARCHAR(512) COMMENT '文件名称',
    subject_type    VARCHAR(64) COMMENT '对象类型（如 file/folder 等）',
    subject_url     VARCHAR(1024) COMMENT '文件URL',
    details         TEXT COMMENT '操作详情（JSON）',
    sync_time       DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '同步时间',
    UNIQUE KEY uk_log_id (log_id),
    INDEX idx_operate_time (operate_time),
    INDEX idx_operator_id (operator_id),
    INDEX idx_action (action)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='云文件操作日志';

-- 云文件相关表（替代原 ods_file_modification）
-- 追加到 schema/init.sql

-- 云盘空间表
CREATE TABLE IF NOT EXISTS ods_file_space (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    workspace_id    VARCHAR(128) NOT NULL COMMENT '空间ID',
    name            VARCHAR(256) COMMENT '空间名称',
    url             VARCHAR(1024) COMMENT '空间URL',
    sync_time       DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '同步时间',
    UNIQUE KEY uk_workspace_id (workspace_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='云盘空间';

-- 云文件记录表（通过 search_dentries 采集）
CREATE TABLE IF NOT EXISTS ods_file_dentry (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    dentry_uuid     VARCHAR(128) NOT NULL COMMENT '文件唯一ID',
    name            VARCHAR(512) COMMENT '文件名称',
    creator_name    VARCHAR(128) COMMENT '创建者姓名',
    creator_user_id VARCHAR(128) COMMENT '创建者UserID',
    modifier_name   VARCHAR(128) COMMENT '最近修改者姓名',
    modifier_user_id VARCHAR(128) COMMENT '最近修改者UserID',
    modified_flag   TINYINT DEFAULT 0 COMMENT '是否被他人修改(creator≠modifier)',
    first_seen      DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '首次发现时间',
    last_sync       DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '最后同步时间',
    UNIQUE KEY uk_dentry_uuid (dentry_uuid),
    INDEX idx_creator (creator_user_id),
    INDEX idx_modifier (modifier_user_id),
    INDEX idx_modified_flag (modified_flag)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='云文件记录';

