"""API to ActualBudget."""

import pathlib
from decimal import Decimal
import logging
import requests
import json
import re
from dataclasses import dataclass
from typing import Dict, List
from actual import Actual
from actual.exceptions import (
    UnknownFileId,
    InvalidFile,
    InvalidZipFile,
    AuthorizationError,
)
from actual.queries import get_accounts, get_account, get_budgets, get_category, create_transaction, get_or_create_account,match_transaction, get_ruleset
from requests.exceptions import ConnectionError, SSLError
import datetime
import threading
from functools import partial

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)

SESSION_TIMEOUT = datetime.timedelta(minutes=30)


@dataclass
class BudgetAmount:
    month: str
    amount: float | None


@dataclass
class Budget:
    name: str
    amounts: List[BudgetAmount]
    balance: Decimal


@dataclass
class Account:
    name: str | None
    balance: Decimal


class ActualBudget:
    """Interfaces to ActualBudget"""

    def __init__(self, hass, endpoint, password, file, cert, encrypt_password, akahu_app_id, akahu_auth_token):
        self.hass = hass
        self.endpoint = endpoint
        self.password = password
        self.file = file
        self.cert = True
        self.encrypt_password = encrypt_password
        self.actual = None
        self.file_id = None
        self.sessionStartedAt = datetime.datetime.now()
        self._lock = threading.Lock()
        self.akahu_app_id = akahu_app_id
        self.akahu_auth_token = akahu_auth_token

    async def get_unique_id(self):
        """Gets a unique id for the sensor based on the remote `file_id`."""
        return await self.hass.async_add_executor_job(self.get_unique_id_sync)

    def get_unique_id_sync(self):
        self.get_session()
        return self.file_id
        
    def get_session(self):
        """Get Actual session if it exists, or create a new one safely."""
        # Invalidate session if it is too old        
        if (
            self.actual
            and self.sessionStartedAt + SESSION_TIMEOUT < datetime.datetime.now()
        ):            
            try:
                self.actual.__exit__(None, None, None)
            except Exception as e:
                _LOGGER.error("Error closing session: %s", e)
            self.actual = None
        
        # Validate existing session
        if self.actual:
            try:
                result = self.actual.validate()
                if not result.data.validated:
                    raise Exception("Session not validated")
                # sync local database
                self.actual.sync()
            except Exception as e:
                _LOGGER.error("Error validating session: %s", e)
                self.actual = None
        
        # Create a new session if needed
        if not self.actual:
        
            self.actual =  self.create_session()
            self.sessionStartedAt = datetime.datetime.now()

        return self.actual.session  # Return session after lock is released

    def create_session(self):        
        _LOGGER.debug("Creating session: url=%s p=*** cert=%s encrypt= %s file=%s",self.endpoint,self.cert,self.encrypt_password,self.file)
        actual = Actual(
            base_url=self.endpoint,
            password=self.password,
            cert=self.cert,
            encryption_password=self.encrypt_password,
            file=self.file,
        )
        
        
        self.file_id = str(actual._file.file_id)
        actual._data_dir = (
            pathlib.Path(self.hass.config.path("actualbudget")) / f"{self.file_id}"
        )
        actual.__enter__()
        result = actual.validate()
        if not result.data.validated:
            raise Exception("Session not validated")
        return actual

    async def get_accounts(self) -> List[Account]:
        """Get accounts."""
        return await self.hass.async_add_executor_job(self.get_accounts_sync)

    def get_accounts_sync(self) -> List[Account]:
        with self._lock:  # Ensure only one thread enters at a time
            session = self.get_session()
            accounts = get_accounts(session)
            return [Account(name=a.name, balance=a.balance) for a in accounts]

    async def get_account(self, account_name) -> Account:
        return await self.hass.async_add_executor_job(
            self.get_account_sync,
            account_name,
        )

    def get_account_sync(
        self,
        account_name,
    ) -> Account:
        with self._lock:  # Ensure only one thread enters at a time
            session = self.get_session()
            account = get_account(session, account_name)
            if not account:
                raise Exception(f"Account {account_name} not found")
            return Account(name=account.name, balance=account.balance)

    async def get_budgets(self) -> List[Budget]:
        """Get budgets."""
        return await self.hass.async_add_executor_job(self.get_budgets_sync)

    def get_budgets_sync(self) -> List[Budget]:
        with self._lock:  # Ensure only one thread enters at a time
            session = self.get_session()
            budgets_raw = get_budgets(session)
            budgets: Dict[str, Budget] = {}
            for budget_raw in budgets_raw:
                if not budget_raw.category:
                    continue
                category = str(budget_raw.category.name)
                amount = (
                    None if not budget_raw.amount else (float(budget_raw.amount) / 100)
                )
                month = str(budget_raw.month)
                if category not in budgets:
                    budgets[category] = Budget(
                        name=category, amounts=[], balance=Decimal(0)
                    )
                budgets[category].amounts.append(
                    BudgetAmount(month=month, amount=amount)
                )
            for category in budgets:
                budgets[category].amounts = sorted(
                    budgets[category].amounts, key=lambda x: x.month
                )
                category_data = get_category(session, category)
                budgets[category].balance = (
                    category_data.balance if category_data else Decimal(0)
                )
            return list(budgets.values())

    async def get_budget(self, budget_name) -> Budget:
        return await self.hass.async_add_executor_job(
            self.get_budget_sync,
            budget_name,
        )

    def get_budget_sync(
        self,
        budget_name,
    ) -> Budget:
        with self._lock:  # Ensure only one thread enters at a time
            session = self.get_session()
            budgets_raw = get_budgets(session, None, budget_name)
            if not budgets_raw or not budgets_raw[0]:
                raise Exception(f"budget {budget_name} not found")
            budget: Budget = Budget(
                name=budget_name, amounts=[], balance=Decimal(0))
            for budget_raw in budgets_raw:
                amount = (
                    None if not budget_raw.amount else (float(budget_raw.amount) / 100)
                )
                month = str(budget_raw.month)
                budget.amounts.append(BudgetAmount(month=month, amount=amount))
            budget.amounts = sorted(budget.amounts, key=lambda x: x.month)
            category_data = get_category(session, budget_name)
            budget.balance = category_data.balance if category_data else Decimal(
                0)
            return budget

    async def run_bank_sync(self) -> None:
        """Run bank synchronization."""
        return await self.hass.async_add_executor_job(self.run_bank_sync_sync)
    
    def run_bank_sync_sync(self) -> None:
        with self._lock:  # Ensure only one thread enters at a time
            self.get_session()

            self.actual.sync()
            self.actual.run_bank_sync()
            self.actual.commit()

    async def run_akahu_bank_sync(self, sync_days, sync_categories) -> None:
        """Run Akahu bank synchronization."""
        return await self.hass.async_add_executor_job(partial(self.run_akahu_bank_sync_sync,sync_days,sync_categories))

    def cleanup_meta(self, checkstring) -> str:
        # exclude cdn web/image links
        if "cdn." in  checkstring.lower():
            return ""
        #ignore any akahu meta data
        if "akahu" in  checkstring.lower():
            return ""        
        return checkstring
    
    def run_akahu_bank_sync_sync(self, sync_days, sync_categories) -> None:
        headers = {
            "accept": "application/json",
            "authorization": self.akahu_auth_token,
            "X-Akahu-Id": self.akahu_app_id
        }
        _LOGGER.debug("run_akahu_bank_sync_sync - Syncing: %s Days, Categories: %s",sync_days, sync_categories) 
        with self._lock:  # Ensure only one thread enters at a time
            session = self.get_session()
            ruleset = get_ruleset(session)
            #self.actual.sync()
            # Start by getting a list of accounts
            url = "https://api.akahu.io/v1/accounts"

            response = requests.get(url, headers=headers)
            #Should prob do some response handling here, but for now this should log the response code. i.e. 200,401 etc
            _LOGGER.debug("response: %s",response)
            json_object = json.loads(response.text)

            
            # For each account in Akahu
            # We will loop over every account, then get every transaction for that account
            for account in json_object["items"]:
                account_name = account["name"]
                account_id = account["_id"]

                # Check if account exists in Actual, if not, create
                act = get_or_create_account(session, account_name)
                
                # Url template for transactions for that account
                url = "https://api.akahu.io/v1/accounts/"+account_id+"/transactions"
            
                # Default to last 20 days
                startdate = (datetime.datetime.now() - datetime.timedelta(days = 20)).isoformat()
                if(sync_days):
                    if(sync_days.lower() == "all"):
                        startdate =""
                    else:
                        try:
                            int_sync_days = int(sync_days)
                            startdate = (datetime.datetime.now() - datetime.timedelta(days = int_sync_days)).isoformat()
                        except ValueError:
                            _LOGGER.warning("Could not parse Akahu Sync days (%s) - defaulting to 20", sync_days)                
                
                # Start with no cursor
                trans_cursor = None 
                trans_total_count = 0
                trans_imported_count = 0
                trans_cursor_count = 0

                # While cursor is not null, keep iterating        
                while True:            
                    queryParams = {}
                    if not startdate =="":  queryParams["start"] =startdate
                    #if not enddate =="":   queryParams["end"] =enddate  # don't think we ever need this 
                    if (trans_cursor):   queryParams["cursor"] =trans_cursor
                    #  Get the transactions for that account, passing through the query parameters above (including cursor if needed)
                    transresponse = requests.get(url,headers=headers,params=queryParams)
                    transjson_obj = json.loads(transresponse.text)
                    trans_cursor = transjson_obj["cursor"]["next"]

                    # For each transaction returned, we're going to match or create in Actual
                    for transaction in transjson_obj["items"]:
                        trans_id = transaction["_id"]
                        trans_date = datetime.datetime.strptime(transaction["date"], "%Y-%m-%dT%H:%M:%S.%f%z").astimezone().date()
                        trans_desc = transaction["description"]
                        trans_amount = transaction["amount"]
                        trans_merchant_summary = None
                        trans_meta_summary = None
                        trans_category = None
                        trans_category_parent = None
                        trans_total_count = trans_total_count+1

                        # Merchant, Meta and Category are not always there, so check first
                        if transaction.get("merchant"):
                            for key, value in transaction["merchant"].items():
                                # We don't care about the _id, but want the rest
                                if(key != "_id"):
                                    merchant_meta = self.cleanup_meta(str(value))      
                                    if trans_merchant_summary is None:
                                        trans_merchant_summary = merchant_meta
                                    else:
                                        trans_merchant_summary += "  " + merchant_meta                        
                        if sync_categories:
                            if transaction.get("category"):                    
                                # This first one is the specific (often _too_ specific) category name
                                trans_category = transaction["category"]["name"]
                                # Below gets the parent category group
                                category_groups = transaction["category"]["groups"]
                                group_names = [group["name"] for group in category_groups.values()]                                
                                trans_category_parent = group_names[0]
                        
                        if transaction.get("meta"):
                            for key, value in transaction["meta"].items():
                                # We don't care about the _id, but want the rest
                                if(key != "_id"):
                                    meta_val = self.cleanup_meta(str(value))                      
                                    if trans_meta_summary is None:
                                        trans_meta_summary = meta_val
                                    else:
                                        trans_meta_summary += "  " + meta_val

                        # Throw all the extra meta data we've gathered into Notes
                        trans_notes = ""
                        if(trans_meta_summary):
                            trans_notes += trans_meta_summary+" "
                        if(trans_merchant_summary):
                            trans_notes += trans_merchant_summary+" "
                        if(trans_category):
                            trans_notes += trans_category+" "
                        trans_notes += trans_desc

                        # Strip numbers from the end of the description (Bank seems to like to add them there?)
                        # Blindly just removing any string that ends with 3 or more numbers
                        # We still copy the original description into notes above though
                        trans_desc =  (re.sub(r'\d{3,}$', '', trans_desc)).strip()
                                
                        # Build transaction Params
                        params = {
                            "s": session,
                            "date": trans_date,
                            "account": account_name,
                            "imported_id": trans_id,
                            "payee": trans_desc,
                            "notes": trans_notes,
                            "category": trans_category_parent,
                            "amount": trans_amount,
                            "cleared": False,
                        }        

                        # Filter out None values
                        filtered_params = {k: v for k, v in params.items() if v is not None}

                        # First check for any match on imported_id = trans_id
                        t = match_transaction(s=session,date=trans_date,account=account_name,imported_id=trans_id)

                        # Only create transactions if there's no match
                        if(t is None):
                            t = create_transaction(**filtered_params)   
                            trans_imported_count = trans_imported_count+1
                            #_LOGGER.debug("Imported transaction: %s", t)
                        #else:
                            # Do some updates here maybe?


                        # Apply the Actual ruleset against our transaction
                        ruleset.run(t)

                    # This will get us out of the While True loop!
                    trans_cursor_count = trans_cursor_count+1
                    if not trans_cursor or trans_cursor.lower() == "none":
                        _LOGGER.info("Account: %s | Total Transactions Processed: %s | New Transactions imported: %s | Pages count: %s",account_name,trans_total_count,trans_imported_count,trans_cursor_count)
                        break
            _LOGGER.debug("committing session")
            self.actual.commit()
    
    async def run_budget_sync(self) -> None:
        """Run bank synchronization."""
        return await self.hass.async_add_executor_job(self.run_budget_sync_sync)

    def run_budget_sync_sync(self) -> None:
        with self._lock:  # Ensure only one thread enters at a time
            self.get_session()

            self.actual.sync()

    async def test_connection(self):
        return await self.hass.async_add_executor_job(self.test_connection_sync)

    def test_connection_sync(self):
        try:
            actualSession = self.get_session()
            if not actualSession:
                return "failed_file"
        # except SSLError:
        #     return "failed_ssl"
        # except ConnectionError:
        #     return "failed_connection"
        # except AuthorizationError:
        #     return "failed_auth"
        # except UnknownFileId:
        #     return "failed_file"
        # except InvalidFile:
        #     return "failed_file"
        # except InvalidZipFile:
        #     return "failed_file"
        except Exception as e:
             _LOGGER.error(f"test_connection_sync failed: {e}")
        return None
