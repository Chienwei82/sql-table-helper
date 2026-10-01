-- Edge-case fixtures for the live suite (02): what the happy-path sample in
-- 01_sample_catalog.sql deliberately does not cover. Idempotent: safe to re-run.
--
-- Coverage added here: referential actions other than CASCADE · a table with no usable
-- row identity · identity from a single-column non-null UNIQUE · PERSISTED computed
-- columns · a non-unique index · identifiers that need quoting · NULL-heavy tables for
-- the write/concurrency tests · types that stress the driver round-trip · a
-- system-versioned (temporal) table · a plain table for the many-rows test.

USE SwissKnifeSample;
GO

-- Schemas other than dbo, one with a space and one with an accent.
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'Lookups')
    EXEC('CREATE SCHEMA [Lookups]');
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'catálogos')
    EXEC(N'CREATE SCHEMA [catálogos]');
GO

-- Referential actions other than CASCADE: ON DELETE SET NULL and the NO ACTION default.
IF OBJECT_ID(N'dbo.Supplier', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Supplier
    (
        SupplierId int IDENTITY(1,1) NOT NULL,
        Name       nvarchar(100)     NOT NULL,
        CONSTRAINT PK_Supplier PRIMARY KEY (SupplierId)
    );
END;
GO

IF OBJECT_ID(N'dbo.Store', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Store
    (
        StoreId int IDENTITY(1,1) NOT NULL,
        Name    nvarchar(100)     NOT NULL,
        CONSTRAINT PK_Store PRIMARY KEY (StoreId)
    );
END;
GO

IF OBJECT_ID(N'dbo.Item', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Item
    (
        ItemId   int IDENTITY(1,1) NOT NULL,
        StoreId  int                NULL,
        Supplier int                NULL,
        Qty      int                NOT NULL CONSTRAINT DF_Item_Qty DEFAULT (1),
        CONSTRAINT PK_Item PRIMARY KEY (ItemId),
        CONSTRAINT FK_Item_Store FOREIGN KEY (StoreId)
            REFERENCES dbo.Store (StoreId) ON DELETE SET NULL,
        CONSTRAINT FK_Item_Supplier FOREIGN KEY (Supplier)
            REFERENCES dbo.Supplier (SupplierId) ON DELETE NO ACTION
    );
END;
GO

-- No usable row identity: no PK, and the only UNIQUE spans two columns. Rows must be
-- read-only (S-4/OQ-5) and writes refused rather than guessed.
IF OBJECT_ID(N'dbo.Keyless', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Keyless
    (
        PartA int NOT NULL,
        PartB int NOT NULL,
        Note  nvarchar(50) NULL,
        CONSTRAINT UQ_Keyless_AB UNIQUE (PartA, PartB)
    );
END;
GO

-- Identity from a single-column non-null UNIQUE rather than a PK (OQ-5).
IF OBJECT_ID(N'dbo.UniqueOnly', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.UniqueOnly
    (
        Code  nvarchar(20) NOT NULL,
        Label nvarchar(50) NULL,
        CONSTRAINT UQ_UniqueOnly_Code UNIQUE (Code)
    );
END;
GO

-- Constraint-light, NULL-heavy table: the fixture for the compare-original-values
-- guard, where a NULL pre-image must become IS NULL and never "= NULL".
IF OBJECT_ID(N'dbo.Simple', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Simple
    (
        Id     int IDENTITY(1,1) NOT NULL,
        Name   nvarchar(100) NULL,
        Qty    int            NULL,
        Active bit            NOT NULL CONSTRAINT DF_Simple_Active DEFAULT (1),
        CONSTRAINT PK_Simple PRIMARY KEY (Id)
    );
END;
GO

-- Types that stress the pyodbc round-trip in both directions.
IF OBJECT_ID(N'dbo.Typed', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Typed
    (
        Id        int IDENTITY(1,1)  NOT NULL,
        TextMax   nvarchar(max)     NULL,
        TextShort nvarchar(50)      NULL,
        Binary    varbinary(64)     NULL,
        BinaryMax varbinary(max)    NULL,
        Amount    decimal(18,4)     NULL,
        Ratio     float             NULL,
        Fixed     numeric(10,2)     NULL,
        Cash      money             NULL,
        Uid       uniqueidentifier  NULL,
        Ts        datetime2(7)      NULL,
        Offset    datetimeoffset(7) NULL,
        Day       date              NULL,
        Moment    time(7)           NULL,
        Flag      bit               NOT NULL,
        Json      nvarchar(max)     NULL,
        CONSTRAINT PK_Typed PRIMARY KEY (Id)
    );
END;
GO

-- Identifiers needing bracketed quoting: a space, a reserved keyword as a column name,
-- a literal ']' inside a name, and an accented schema.
-- The name needs bracket-escaping on both sides: in the CREATE/INSERT below and in
-- OBJECT_ID, which also parses its argument as an identifier path.
IF OBJECT_ID(N'[Lookups].[Weird ]]Name]', N'U') IS NULL
BEGIN
    CREATE TABLE [Lookups].[Weird ]]Name]
    (
        [Col with space] int IDENTITY(1,1) NOT NULL,
        [select]         nvarchar(50)       NULL,
        [Cola]]B]        int               NULL,
        CONSTRAINT [PK Weird ]]Name] PRIMARY KEY ([Col with space])
    );
END;
GO

IF OBJECT_ID(N'catálogos.Moneda', N'U') IS NULL
BEGIN
    CREATE TABLE [catálogos].[Moneda]
    (
        [Código]      char(3)      NOT NULL,
        [Descripción] nvarchar(80) NULL,
        CONSTRAINT [PK Moneda] PRIMARY KEY ([Código])
    );
END;
GO

-- PERSISTED computed column plus a non-unique index (neither appears in 01).
IF OBJECT_ID(N'dbo.OrderLine', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.OrderLine
    (
        LineId int IDENTITY(1,1) NOT NULL,
        Qty    int             NOT NULL,
        Price  decimal(10,2)   NOT NULL,
        Total  AS (Qty * Price) PERSISTED,
        CONSTRAINT PK_OrderLine PRIMARY KEY (LineId)
    );
END;
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = N'IX_OrderLine_Qty' AND object_id = OBJECT_ID(N'dbo.OrderLine'))
    CREATE INDEX IX_OrderLine_Qty ON dbo.OrderLine (Qty);
GO

-- System-versioned (temporal) table: exercises is_system_versioned + history pairing.
IF OBJECT_ID(N'dbo.Account', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Account
    (
        AccountId int IDENTITY(1,1) NOT NULL,
        Balance   decimal(12,2)   NOT NULL CONSTRAINT DF_Account_Balance DEFAULT (0),
        ValidFrom datetime2 GENERATED ALWAYS AS ROW START NOT NULL
            CONSTRAINT DF_Account_ValidFrom DEFAULT SYSUTCDATETIME(),
        ValidTo   datetime2 GENERATED ALWAYS AS ROW END   NOT NULL
            CONSTRAINT DF_Account_ValidTo DEFAULT CONVERT(datetime2, '9999-12-31 23:59:59.99'),
        -- SYSTEM_VERSIONING requires a primary key; period columns must not be part of it.
        CONSTRAINT PK_Account PRIMARY KEY (AccountId),
        PERIOD FOR SYSTEM_TIME (ValidFrom, ValidTo)
    ) WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = dbo.AccountHistory));
END;
GO

-- Plain table for the many-rows / parameter-limit test.
-- Plain table for the many-rows / parameter-limit test. BULK is a reserved keyword,
-- so the table name must be bracketed everywhere it appears.
IF OBJECT_ID(N'[dbo].[Bulk]', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[Bulk]
    (
        Id   int IDENTITY(1,1) NOT NULL,
        Name nvarchar(50)      NOT NULL,
        CONSTRAINT PK_Bulk PRIMARY KEY (Id)
    );
END;
GO

-- Seed data, each block guarded so re-runs do not duplicate rows.
IF NOT EXISTS (SELECT 1 FROM dbo.Supplier)
    INSERT INTO dbo.Supplier (Name) VALUES (N'Acme');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Store)
    INSERT INTO dbo.Store (Name) VALUES (N'Berlin'), (N'Madrid');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Item)
    INSERT INTO dbo.Item (StoreId, Supplier, Qty) VALUES (1, 1, 10), (1, 1, 5);
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Keyless)
    INSERT INTO dbo.Keyless (PartA, PartB, Note) VALUES (1, 1, N'first'), (1, 2, N'second');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.UniqueOnly)
    INSERT INTO dbo.UniqueOnly (Code, Label) VALUES (N'EUR', N'Euro'), (N'USD', N'Dollar');
GO

-- One row with a NULL Qty and one populated: the NULL-pre-image case.
IF NOT EXISTS (SELECT 1 FROM dbo.Simple)
    INSERT INTO dbo.Simple (Name, Qty) VALUES (N'with nulls', NULL), (N'populated', 7);
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Typed)
    INSERT INTO dbo.Typed (TextMax, TextShort, Binary, Amount, Ratio, Fixed, Cash, Uid,
                           Ts, Offset, Day, Moment, Flag, Json)
    VALUES (N'texto largo', N'corta, con coma y "comillas"', 0x00FF10,
            12345.6789, 0.5, 99.99, 12.34, '3F2504E0-4F89-11D3-9A0C-0305E82C3301',
            '2024-02-29 13:45:56.1234567', '2024-02-29 13:45:56.1234567 +02:00',
            '2024-02-29', '13:45:56.1234567', 1, N'{"k": "v"}');
GO

-- [Col with space] is IDENTITY, so it is left to the server.
IF NOT EXISTS (SELECT 1 FROM [Lookups].[Weird ]]Name])
    INSERT INTO [Lookups].[Weird ]]Name] ([select], [Cola]]B])
    VALUES (N'primero', 42);
GO

IF NOT EXISTS (SELECT 1 FROM [catálogos].[Moneda])
    INSERT INTO [catálogos].[Moneda] ([Código], [Descripción]) VALUES (N'EUR', N'euro');
GO

IF NOT EXISTS (SELECT 1 FROM dbo.OrderLine)
    INSERT INTO dbo.OrderLine (Qty, Price) VALUES (2, 10.50), (3, 1.25);
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Account)
    INSERT INTO dbo.Account (Balance) VALUES (100.00);
GO

SELECT 'SwissKnifeSample edge fixtures ready' AS status;
GO
