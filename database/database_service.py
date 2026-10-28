from arango import ArangoClient
from arango.exceptions import CollectionCreateError
from database.config import ARANGO_HOST, ARANGO_USER, ARANGO_PASS, ARANGO_DB

class DatabaseService:
    def __init__(self, db_name=None):
        # Use variables from config.py
        self.host = ARANGO_HOST
        self.username = ARANGO_USER
        self.password = ARANGO_PASS
        self.db_name = db_name if db_name else ARANGO_DB
        
        # Initialize Client
        self.client = ArangoClient(hosts=self.host)
        
        # Connect to System DB to check/create target DB
        self.sys_db = self.client.db('_system', username=self.username, password=self.password)
        self._setup_db()
        
        # Connect to actual DB
        self.db = self.client.db(self.db_name, username=self.username, password=self.password)
        self._setup_collections()

    def _setup_db(self):
        if not self.sys_db.has_database(self.db_name):
            self.sys_db.create_database(self.db_name)

    
    def _setup_collections(self):
        db = self.client.db(self.db_name, username=self.username, password=self.password)
        
        # Document collections
        for coll in ['Books', 'Topics', 'Metadata']:
            try:
                db.create_collection(coll)
            except CollectionCreateError:
                pass  # already exists → ignore
        
        # Edge collection
        try:
            db.create_collection('has_topic', edge=True)
        except CollectionCreateError:
            pass

    def truncate_collections(self):
        for coll_name in ['Books', 'Topics', 'has_topic', 'Metadata']:
            if self.db.has_collection(coll_name):
                self.db.collection(coll_name).truncate()

    def execute_aql(self, query, bind_vars=None):
        return self.db.aql.execute(query, bind_vars=bind_vars)

    def insert_document(self, collection, document):
        if not self.db.has_collection(collection):
            self.db.create_collection(collection)
        return self.db.collection(collection).insert(document, overwrite=True)

    def insert_edge(self, collection, from_id, to_id, data=None):
        if not self.db.has_collection(collection):
            self.db.create_collection(collection, edge=True)
        
        edge = {'_from': from_id, '_to': to_id}
        if data:
            edge.update(data)
        return self.db.collection(collection).insert(edge, overwrite=True)