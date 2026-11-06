"""Provision local synthetic SmartSupport accounts without storing passwords."""
import argparse
from getpass import getpass

from auth_store import AuthStore, CUSTOMER_IDS
from runtime_policy import web_data_directory
from backup_manager import runtime_lease


def main(argv=None):
    parser=argparse.ArgumentParser(description="Create a local SmartSupport test account")
    parser.add_argument("role",choices=("customer","operator"))
    parser.add_argument("username")
    parser.add_argument("--customer-id",choices=sorted(CUSTOMER_IDS),
                        help="Required for customer accounts; forbidden for operators")
    args=parser.parse_args(argv)
    if args.role=="customer" and not args.customer_id:
        parser.error("customer accounts require --customer-id")
    if args.role=="operator" and args.customer_id:
        parser.error("operator accounts cannot own customer data")
    password=getpass("New password (12-256 characters): ")
    confirm=getpass("Confirm password: ")
    if password != confirm:
        parser.error("Passwords did not match")
    try:
        with runtime_lease():
            AuthStore(web_data_directory()).create_user(args.username,password,role=args.role,
                                                         customer_id=args.customer_id)
    except Exception as exc:
        parser.error(str(exc))
    print("Account created in the local runtime database.")


if __name__=="__main__":
    main()
