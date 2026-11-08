from sqlalchemy import text
from app.database import engine

try:
    with engine.connect() as connection:

        result = connection.execute(
            text("SELECT version(), current_database()")
        )

        version, database = result.fetchone()

        print("\nCONNECTED TO NEON SUCCESSFULLY!")
        print("Database:", database)
        print("PostgreSQL:", version)

except Exception as error:
    print("\nDATABASE CONNECTION FAILED")
    print(error)