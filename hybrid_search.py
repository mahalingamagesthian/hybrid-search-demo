from typing import Any, Dict, List, Tuple
import streamlit as st
from langchain_couchbase.vectorstores import CouchbaseSearchVectorStore
import os
from langchain_openai import OpenAIEmbeddings
from couchbase.cluster import Cluster
from couchbase.auth import PasswordAuthenticator
from couchbase.options import ClusterOptions
from datetime import timedelta
import couchbase.search as search
from couchbase.options import SearchOptions
from couchbase.vector_search import VectorQuery, VectorSearch
from openai import OpenAI


def check_environment_variable(variable_name):
    """Check if environment variable is set"""
    if variable_name not in os.environ:
        st.error(
            f"{variable_name} environment variable is not set. Please add it to the secrets.toml file"
        )
        st.stop()


def generate_embeddings(client, input_data):
    """Generate OpenAI embeddings for the input data"""
    response = client.embeddings.create(input=input_data, model=EMBEDDING_MODEL)
    return response.data[0].embedding


def cleanup_poster_url(poster_url):
    """Convert from https://m.media-amazon.com/images/M/MV5BMDFkYTc0MGEtZmNhMC00ZDIzLWFmNTEtODM1ZmRlYWMwMWFmXkEyXkFqcGdeQXVyMTMxODk2OTU@._V1_UX67_CR0,0,67,98_AL_.jpg to https://m.media-amazon.com/images/M/MV5BMDFkYTc0MGEtZmNhMC00ZDIzLWFmNTEtODM1ZmRlYWMwMWFmXkEyXkFqcGdeQXVyMTMxODk2OTU@._V1_.jpg"""

    prefix = poster_url.split("_V1_")[0]
    suffix = poster_url.split("_AL_")[1]

    return prefix + suffix


@st.cache_resource(show_spinner="Connecting to Couchbase")
def connect_to_couchbase(connection_string, db_username, db_password):
    """Connect to couchbase"""

    auth = PasswordAuthenticator(db_username, db_password)
    options = ClusterOptions(auth)
    connect_string = connection_string
    cluster = Cluster(connect_string, options)

    # Wait until the cluster is ready for use.
    cluster.wait_until_ready(timedelta(seconds=5))

    return cluster


@st.cache_resource(show_spinner="Connecting to Vector Store")
def get_couchbase_vector_store(
    _cluster,
    db_bucket,
    db_scope,
    db_collection,
    _embedding,
    index_name,
    text_key,
    embedding_key,
) -> CouchbaseSearchVectorStore:
    """Return the Couchbase vector store"""
    vector_store = CouchbaseSearchVectorStore(
        cluster=_cluster,
        bucket_name=db_bucket,
        scope_name=db_scope,
        collection_name=db_collection,
        embedding=_embedding,
        index_name=index_name,
        text_key=text_key,
        embedding_key=embedding_key,
    )
    return vector_store


@st.cache_resource
def create_filter(
    year_range: Tuple[int], rating: float
) -> Tuple[search.SearchQuery, str]:
    """Create a prefilter for the vector search"""
    # Fields in the document used for search
    year_field = "Released_Year"
    rating_field = "IMDB_Rating"

    filter_descriptions = []
    filter_operations = []

    if year_range:
        year_query = search.NumericRangeQuery(
            field=year_field,
            min=year_range[0],
            max=year_range[1],
            inclusive_min=True,
            inclusive_max=True,
        )
        filter_operations.append(year_query)
        filter_descriptions.append(f"""NumericRangeQuery(
            field={year_field},
            min={year_range[0]},
            max={year_range[1]},
            inclusive_min=True,
            inclusive_max=True
        )""") 
    if rating:
        rating_query = search.NumericRangeQuery(
            field=rating_field,
            min=rating,
            inclusive_min=False,
        )
        filter_operations.append(rating_query)
        filter_descriptions.append(f"""NumericRangeQuery(
            field={rating_field},
            min={rating},
            inclusive_min=False
        )""")

    filters = search.ConjunctionQuery(*filter_operations)

    if len(filter_descriptions) > 1:
        filter_descriptions = "ConjunctionQuery(" + ", ".join(filter_descriptions) + " )"
    else:
        filter_descriptions = filter_descriptions[0]

    return filters, filter_descriptions



def search_couchbase(
    db_scope: Any,
    index_name: str,
    embedding_client: Any,
    embedding_key: str,
    search_text: str,
    k: int = 5,
    fields: List[str] = ["*"],
    search_filters: search.SearchQuery = None,
):
    """Hybrid search using Python SDK in couchbase"""
    # Generate vector embeddings to search with
    search_embedding = generate_embeddings(embedding_client, search_text)

    # Create the search request
    search_req = search.SearchRequest.create(
        VectorSearch.from_vector_query(
            VectorQuery(
                field_name=embedding_key,
                vector=search_embedding,
                prefilter=search_filters,
                num_candidates=k,
            )
        )
    )

    docs_with_score = []

    try:
        # Perform the search
        search_iter = db_scope.search(
            index_name,
            search_req,
            SearchOptions(
                limit=k,
                fields=fields,
            ),
        )

        # Parse the results
        for row in search_iter.rows():
            score = row.score
            docs_with_score.append((row.fields, score))
    except Exception as e:
        raise e

    return docs_with_score


if __name__ == "__main__":
    st.set_page_config(
        page_title="Movie Search",
        page_icon="🎥",
        layout="centered",
        initial_sidebar_state="auto",
        menu_items=None,
    )

    # Load environment variables
    DB_CONN_STR = os.getenv("DB_CONN_STR")
    DB_USERNAME = os.getenv("DB_USERNAME")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_BUCKET = os.getenv("DB_BUCKET")
    DB_SCOPE = os.getenv("DB_SCOPE")
    DB_COLLECTION = os.getenv("DB_COLLECTION")
    INDEX_NAME = os.getenv("INDEX_NAME")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")

    # Use text-embedding-3-small as the embedding model if not set
    if not EMBEDDING_MODEL:
        EMBEDDING_MODEL = "text-embedding-3-small"

    # Ensure that all environment variables are set
    check_environment_variable("OPENAI_API_KEY")
    check_environment_variable("DB_CONN_STR")
    check_environment_variable("DB_USERNAME")
    check_environment_variable("DB_PASSWORD")
    check_environment_variable("DB_BUCKET")
    check_environment_variable("DB_SCOPE")
    check_environment_variable("DB_COLLECTION")
    check_environment_variable("INDEX_NAME")

    # Initialize empty filters
    search_filters = {}

    # Native OpenAI library for generating embeddings
    openai_embedding_client = OpenAI()

    # Use OpenAI Embeddings from LangChain
    embedding = OpenAIEmbeddings(model=EMBEDDING_MODEL)

    # Connect to Couchbase Vector Store
    cluster = connect_to_couchbase(DB_CONN_STR, DB_USERNAME, DB_PASSWORD)
    bucket = cluster.bucket(DB_BUCKET)
    scope = bucket.scope(DB_SCOPE)

    # Create the LangChain Couchbase Vector Store object
    vector_store = get_couchbase_vector_store(
        cluster,
        DB_BUCKET,
        DB_SCOPE,
        DB_COLLECTION,
        embedding,
        INDEX_NAME,
        text_key="Overview",
        embedding_key="Overview_embedding",
    )

    search_filters = None
    search_filter_descriptions = None

    # UI Elements
    text = st.text_input("Find your movie")
    with st.sidebar:
        st.header("Search Options")
        is_langchain = st.checkbox("Use LangChain")
        no_of_results = st.number_input(
            "Number of results", min_value=1, value=5, format="%i"
        )
        # Filters
        st.subheader("Filters")
        enable_filters = st.checkbox("Enable filters")

        if enable_filters:
            year_range = st.slider("Released Year", 1900, 2024, (1900, 2024))
            rating = st.number_input("Minimum IMDB Rating", 0.0, 10.0, 0.0, step=1.0)
            show_filter = st.checkbox("Show filter")
            search_filters, search_filter_descriptions = create_filter(year_range, rating)
            if show_filter:
                st.write(search_filter_descriptions)

    submit = st.button("Submit")

    if submit:
        # Search using the LangChain interface
        if is_langchain:
            # Perform the search using LangChain
            docs = vector_store.similarity_search_with_score(
                text,
                k=no_of_results,
                filter=search_filters,
            )

            for doc in docs:
                movie, score = doc

                # Display the results in a grid
                st.header(movie.metadata["Series_Title"])
                col1, col2 = st.columns(2)
                with col1:
                    st.image(
                        cleanup_poster_url(movie.metadata["Poster_Link"]),
                        use_container_width=True,
                    )
                with col2:
                    st.write("Synopsis:", movie.page_content)
                    st.write(f"Score: {score:.{3}f}")
                    st.write("Released Year:", movie.metadata["Released_Year"])
                    st.write("IMDB Rating:", movie.metadata["IMDB_Rating"])
                    st.write("Runtime:", movie.metadata["Runtime"])
                st.divider()

        # Search using the Couchbase Python SDK
        else:
            # Perform the search using the Couchbase Python SDK
            results = search_couchbase(
                scope,
                INDEX_NAME,
                openai_embedding_client,
                "Overview_embedding",
                text,
                k=no_of_results,
                search_filters=search_filters,
            )
            for doc in results:
                movie, score = doc

                # Display the results in a grid
                st.header(movie["Series_Title"])
                col1, col2 = st.columns(2)
                with col1:
                    st.image(
                        cleanup_poster_url(movie["Poster_Link"]),
                        use_container_width=True,
                    )
                with col2:
                    st.write("Synopsis:", movie["Overview"])
                    st.write(f"Score: {score:.{3}f}")
                    st.write("Released Year:", movie["Released_Year"])
                    st.write("IMDB Rating:", movie["IMDB_Rating"])
                    st.write("Runtime:", movie["Runtime"])
                st.divider()
