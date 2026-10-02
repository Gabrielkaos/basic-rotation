-- One table the API reads, and the two logins that may read it. They have
-- the same rights; only one of them is in use at a time.
CREATE TABLE greeting (
    id serial PRIMARY KEY,
    text text NOT NULL
);
INSERT INTO greeting (text) VALUES ('hello from the database');

-- Blue works from the start. Green exists but has no password, so nobody can
-- log in as it until the first rotation gives it one.
CREATE ROLE app_blue LOGIN PASSWORD :'blue_password';
CREATE ROLE app_green LOGIN;

GRANT CONNECT ON DATABASE demo TO app_blue, app_green;
GRANT SELECT ON greeting TO app_blue, app_green;
