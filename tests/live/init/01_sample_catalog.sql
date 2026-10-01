-- Sample catalog-like database for sql-table-swiss-knife (M1 test fixture).
-- Loaded by tests/live/docker-compose.yml via sqlcmd. Idempotent: safe to re-run.
--
-- Coverage: identity PK · composite PK · FKs · self-referencing FK · computed column ·
-- rowversion column · defaults · CHECK · UNIQUE · AFTER trigger · INSTEAD OF trigger.

IF DB_ID(N'SwissKnifeSample') IS NULL
    CREATE DATABASE SwissKnifeSample;
GO

USE SwissKnifeSample;
GO

IF OBJECT_ID(N'dbo.Country', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Country
    (
        Code        char(2)        NOT NULL,
        Name        nvarchar(100)  NOT NULL,
        Iso3        char(3)        NULL,
        Population  int            NOT NULL CONSTRAINT DF_Country_Population DEFAULT (0),
        IsActive    bit            NOT NULL CONSTRAINT DF_Country_IsActive DEFAULT (1),
        CreatedUtc  datetime2(3)   NOT NULL CONSTRAINT DF_Country_CreatedUtc DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_Country PRIMARY KEY (Code),
        CONSTRAINT UQ_Country_Name UNIQUE (Name),
        -- COLLATE ... CS is required: the database default collation is case-insensitive,
        -- where "Code = UPPER(Code)" is true for *every* value and the CHECK never fires.
        CONSTRAINT CK_Country_Code CHECK (Code COLLATE Latin1_General_100_BIN2 = UPPER(Code)),
        CONSTRAINT CK_Country_Population CHECK (Population >= 0)
    );
END;
GO

IF OBJECT_ID(N'dbo.Region', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Region
    (
        RegionId        int IDENTITY(1,1) NOT NULL,
        CountryCode     char(2)          NOT NULL,
        ParentRegionId  int              NULL,
        Name            nvarchar(100)    NOT NULL,
        NameUpper       AS (UPPER(Name)),                     -- computed column
        RowVer          rowversion,                           -- rowversion column
        SortOrder       int              NOT NULL CONSTRAINT DF_Region_SortOrder DEFAULT (0),
        CONSTRAINT PK_Region PRIMARY KEY (RegionId),
        CONSTRAINT FK_Region_Country FOREIGN KEY (CountryCode)
            REFERENCES dbo.Country (Code) ON DELETE CASCADE,
        CONSTRAINT FK_Region_Parent FOREIGN KEY (ParentRegionId)
            REFERENCES dbo.Region (RegionId),                 -- self-referencing FK
        CONSTRAINT CK_Region_Name CHECK (Name <> N'')
    );
END;
GO

IF OBJECT_ID(N'dbo.RegionAlias', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.RegionAlias
    (
        RegionId   int           NOT NULL,
        Lang       char(2)       NOT NULL,
        Label      nvarchar(100) NOT NULL,
        CreatedUtc datetime2(3)  NOT NULL CONSTRAINT DF_RegionAlias_CreatedUtc DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_RegionAlias PRIMARY KEY (RegionId, Lang),   -- composite PK
        CONSTRAINT FK_RegionAlias_Region FOREIGN KEY (RegionId)
            REFERENCES dbo.Region (RegionId) ON DELETE CASCADE
    );
END;
GO

IF OBJECT_ID(N'dbo.AuditLog', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.AuditLog
    (
        AuditId    int IDENTITY(1,1) NOT NULL,
        TableName  nvarchar(128)     NOT NULL,
        Action     nvarchar(16)      NOT NULL,
        RowKey     nvarchar(64)      NOT NULL,
        Detail     nvarchar(400)     NULL,
        ChangedUtc datetime2(3)      NOT NULL CONSTRAINT DF_AuditLog_ChangedUtc DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_AuditLog PRIMARY KEY (AuditId)
    );
END;
GO

-- AFTER trigger: log Region updates into the audit table.
CREATE OR ALTER TRIGGER dbo.trg_Region_AfterUpdate
ON dbo.Region
AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO dbo.AuditLog (TableName, Action, RowKey, Detail)
    SELECT N'Region',
           N'UPDATE',
           CAST(i.RegionId AS nvarchar(64)),
           N'Name: ' + ISNULL(d.Name, N'?') + N' -> ' + i.Name
      FROM inserted AS i
      JOIN deleted  AS d ON d.RegionId = i.RegionId;
END;
GO

-- Updatable catalog view with an INSTEAD OF UPDATE trigger.
CREATE OR ALTER VIEW dbo.v_Country
AS
    SELECT Code, Name, Population, IsActive
      FROM dbo.Country;
GO

CREATE OR ALTER TRIGGER dbo.trg_v_Country_InsteadOfUpdate
ON dbo.v_Country
INSTEAD OF UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE c
       SET c.Name       = i.Name,
           c.Population = i.Population,
           c.IsActive   = i.IsActive
      FROM dbo.Country AS c
      JOIN inserted    AS i ON i.Code = c.Code;
END;
GO

-- Seed data (guarded so re-runs do not duplicate rows).
IF NOT EXISTS (SELECT 1 FROM dbo.Country)
BEGIN
    INSERT INTO dbo.Country (Code, Name, Iso3, Population) VALUES (N'DE', N'Germany',   N'DEU', 83000000);
    INSERT INTO dbo.Country (Code, Name, Iso3, Population) VALUES (N'FR', N'France',    N'FRA', 67000000);
    INSERT INTO dbo.Country (Code, Name, Iso3, Population) VALUES (N'JP', N'Japan',     N'JPN', 125000000);
END;
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Region)
BEGIN
    INSERT INTO dbo.Region (CountryCode, ParentRegionId, Name, SortOrder)
         VALUES (N'DE', NULL, N'Bavaria', 1);
    DECLARE @bavaria int = CAST(SCOPE_IDENTITY() AS int);
    INSERT INTO dbo.Region (CountryCode, ParentRegionId, Name, SortOrder)
         VALUES (N'DE', @bavaria, N'Munich', 2);
    INSERT INTO dbo.Region (CountryCode, ParentRegionId, Name, SortOrder)
         VALUES (N'FR', NULL, N'Normandy', 3);
    INSERT INTO dbo.Region (CountryCode, ParentRegionId, Name, SortOrder)
         VALUES (N'JP', NULL, N'Kanto', 4);

    INSERT INTO dbo.RegionAlias (RegionId, Lang, Label)
         SELECT RegionId, N'de', Name FROM dbo.Region WHERE Name = N'Bavaria';
    INSERT INTO dbo.RegionAlias (RegionId, Lang, Label)
         SELECT RegionId, N'en', N'Bavaria (EN)' FROM dbo.Region WHERE Name = N'Bavaria';
    INSERT INTO dbo.RegionAlias (RegionId, Lang, Label)
         SELECT RegionId, N'fr', N'Normandie' FROM dbo.Region WHERE Name = N'Normandy';
END;
GO

SELECT 'SwissKnifeSample ready' AS status;
GO
