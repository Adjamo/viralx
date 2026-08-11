import time
import json
import os
import re
import getpass
from datetime import datetime, timedelta, timezone
import tweepy

#pip install --no-cache-dir solana==0.34.2 solders==0.21.0 tweepy base58
#pip3 install --no-cache-dir solana==0.34.2 solders==0.21.0 tweepy base58

# Solana imports
from solana.rpc.api import Client as SolanaClient
from solders.keypair import Keypair # type: ignore
from solders.pubkey import Pubkey # type: ignore
from spl.token.instructions import transfer_checked, TransferCheckedParams
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address
from solana.transaction import Transaction

# ==========================================
# CONFIGURATION
# ==========================================
TWITTER_BEARER_TOKEN = "YOUR_TWITTER_BEARER_TOKEN"
TWITTER_API_KEY = "YOUR_API_KEY"
TWITTER_API_SECRET = "YOUR_API_SECRET"
TWITTER_ACCESS_TOKEN = "YOUR_ACCESS_TOKEN"
TWITTER_ACCESS_SECRET = "YOUR_ACCESS_SECRET"

SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
VIRALX_MINT_ADDRESS = "YOUR_VIRALX_TOKEN_MINT_ADDRESS"
VIRALX_DECIMALS = 9  # Adjust this if your SPL token uses different decimals

# File Paths
PAYOUT_FILE = "payout_count.txt"
WINNERS_FILE = "winners_log.json"
TX_LOG_FILE = "tx_signatures.txt"

# Constants
BASE_REWARD = 1_000_000
HALVING_INTERVAL = 500
MAX_COOLDOWN_ENTRIES = 48  # 24 hours * 2 winners per hour

# Regex to find Solana addresses (Base58, 32-44 characters)
SOL_ADDRESS_REGEX = r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"

# ==========================================
# SETUP & INITIALIZATION
# ==========================================
print("🚀 Initializing ViralX Bot...")

# Securely prompt for the private key without echoing it to the terminal
private_key_input = getpass.getpass("Paste your Solana Private Key (Base58) - it will be hidden: ")
payer_keypair = Keypair.from_base58_string(private_key_input)
print(f"✅ Loaded Wallet: {payer_keypair.pubkey()}")

# Initialize Twitter client
twitter_client = tweepy.Client(
    bearer_token=TWITTER_BEARER_TOKEN,
    consumer_key=TWITTER_API_KEY,
    consumer_secret=TWITTER_API_SECRET,
    access_token=TWITTER_ACCESS_TOKEN,
    access_token_secret=TWITTER_ACCESS_SECRET
)

# Initialize Solana client
solana_client = SolanaClient(SOLANA_RPC_URL)
viralx_mint = Pubkey.from_string(VIRALX_MINT_ADDRESS)

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def get_payout_count():
    if not os.path.exists(PAYOUT_FILE):
        with open(PAYOUT_FILE, 'w') as f:
            f.write("0")
        return 0
    with open(PAYOUT_FILE, 'r') as f:
        try:
            return int(f.read().strip())
        except ValueError:
            return 0

def update_payout_count(count):
    with open(PAYOUT_FILE, 'w') as f:
        f.write(str(count))

def get_winners_log():
    if not os.path.exists(WINNERS_FILE):
        with open(WINNERS_FILE, 'w') as f:
            json.dump([], f) # Now starts as an empty list
        return []
    with open(WINNERS_FILE, 'r') as f:
        return json.load(f)

def update_winners_log(log):
    with open(WINNERS_FILE, 'w') as f:
        json.dump(log, f)

def log_transaction(signature):
    with open(TX_LOG_FILE, 'a') as f:
        f.write(f"{signature}\n")

def calculate_reward(payout_count):
    halvings = payout_count // HALVING_INTERVAL
    reward = BASE_REWARD / (2 ** halvings)
    return int(reward)

def extract_sol_address(text):
    matches = re.findall(SOL_ADDRESS_REGEX, text)
    for match in matches:
        try:
            # Basic validation to ensure it's a valid pubkey
            Pubkey.from_string(match)
            return match
        except Exception:
            continue
    return None

def send_viralx_token(destination_address, amount):
    print(f"💸 Initiating transfer of {amount} ViralX to {destination_address}...")
    dest_pubkey = Pubkey.from_string(destination_address)
    
    # Get associated token accounts (ATA)
    source_ata = get_associated_token_address(payer_keypair.pubkey(), viralx_mint)
    dest_ata = get_associated_token_address(dest_pubkey, viralx_mint)
    
    # Create transfer instruction
    transfer_amount = int(amount * (10 ** VIRALX_DECIMALS))
    transfer_ix = transfer_checked(
        TransferCheckedParams(
            program_id=TOKEN_PROGRAM_ID,
            source=source_ata,
            mint=viralx_mint,
            dest=dest_ata,
            owner=payer_keypair.pubkey(),
            amount=transfer_amount,
            decimals=VIRALX_DECIMALS,
            signers=[]
        )
    )
    
    # Build and send transaction
    txn = Transaction().add(transfer_ix)
    recent_blockhash = solana_client.get_latest_blockhash().value.blockhash
    txn.recent_blockhash = recent_blockhash
    txn.sign(payer_keypair)
    
    response = solana_client.send_transaction(txn)
    signature = response.value
    return signature

# ==========================================
# MAIN LOOP
# ==========================================
def run_hourly_competition():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🕒 Starting hourly competition cycle...")
    
    # Calculate time window (last 1 hour)
    start_time = datetime.now(timezone.utc) - timedelta(hours=1)
    
    query = "#viralX @viralxcoin -is:retweet"
    
    print(f"🔍 Searching Twitter for query: {query}")
    try:
        tweets = twitter_client.search_recent_tweets(
            query=query,
            start_time=start_time,
            tweet_fields=["public_metrics", "created_at", "author_id"],
            max_results=100
        )
    except Exception as e:
        print(f"❌ Error fetching tweets: {e}")
        return

    if not tweets.data:
        print("🤷‍♂️ No eligible tweets found this hour.")
        return

    # Process and score tweets
    scored_tweets = []
    winners_log = get_winners_log()
    
    for tweet in tweets.data:
        text = tweet.text
        author_id = str(tweet.author_id)
        
        # Check 24-hour cooldown using the rolling list
        if author_id in winners_log:
            print(f"⏭️ Skipping user {author_id} - they recently won.")
            continue
                
        sol_address = extract_sol_address(text)
        if not sol_address:
            print(f"⏭️ Skipping tweet {tweet.id} - no valid Solana address found.")
            continue
            
        metrics = tweet.public_metrics
        score = (metrics['retweet_count'] * 10) + (metrics['reply_count'] * 3) + metrics['like_count']
        
        scored_tweets.append({
            'tweet_id': tweet.id,
            'author_id': author_id,
            'score': score,
            'created_at': tweet.created_at,
            'sol_address': sol_address
        })

    if not scored_tweets:
        print("🤷‍♂️ No eligible tweets with Solana addresses found this hour.")
        return

    # Sort by Score (Desc), then by Created At (Asc - oldest wins tie)
    scored_tweets.sort(key=lambda x: (x['score'], -x['created_at'].timestamp()), reverse=True)
    
    print(f"📊 Found {len(scored_tweets)} eligible tweets. Top scorers:")
    for i, t in enumerate(scored_tweets[:2]):
        print(f"   {i+1}. Tweet ID: {t['tweet_id']} | Score: {t['score']} | Address: {t['sol_address']}")

    top_tweet = scored_tweets[0]
    second_tweet = scored_tweets[1] if len(scored_tweets) > 1 else None
    
    # Process Payout for 1st Place
    payout_count = get_payout_count()
    reward_amount = calculate_reward(payout_count)
    
    print(f"🏆 1st Place Processing... Reward: {reward_amount:,.0f} ViralX")
    
    first_place_text = (
        f"🥇 WINNING TWEET THIS HOUR! 🏆\n"
        f"{reward_amount:,.0f} VIRALX coins sent to:\n"
        f"{top_tweet['sol_address']}\n"
        f"You won it. 🥇🔥\n"
        f"#ViralX\n\n"
        f"Score: {top_tweet['score']} ((retweets * 10) + (replies * 3) + likes).\n"
        f"Scoring is based on the counts observed when the hourly competition closes."
    )
    
    try:
        tx_sig = send_viralx_token(top_tweet['sol_address'], reward_amount)
        print(f"✅ Transaction Successful! Signature: {tx_sig}")
        log_transaction(tx_sig)
        
        twitter_client.create_tweet(text=first_place_text, in_reply_to_tweet_id=top_tweet['tweet_id'])
        print(f"✅ Replied to 1st place tweet {top_tweet['tweet_id']}")
        
        # Update states and rolling list
        update_payout_count(payout_count + 1)
        winners_log.append(top_tweet['author_id'])
        if len(winners_log) > MAX_COOLDOWN_ENTRIES:
            winners_log.pop(0)
        update_winners_log(winners_log)
        
    except Exception as e:
        print(f"❌ Error during 1st place payout/reply: {e}")

    # Process 2nd Place Reply
    if second_tweet:
        print(f"🥈 2nd Place Processing...")
        second_place_text = (
            f"🥈 2ND PLACE!\n"
            f"You came 2nd this hour with a score of {second_tweet['score']}! 🔥\n"
            f"So close to the top spot... better luck next hour! \n"
            f"#ViralX\n\n"
            f"Scoring metric: ((retweets * 10) + (replies * 3) + likes).\n"
            f"Scoring is based on the counts observed when the hourly competition closes."
        )
        try:
            twitter_client.create_tweet(text=second_place_text, in_reply_to_tweet_id=second_tweet['tweet_id'])
            print(f"✅ Replied to 2nd place tweet {second_tweet['tweet_id']}")
            
            # Add 2nd place to cooldown log
            winners_log.append(second_tweet['author_id'])
            if len(winners_log) > MAX_COOLDOWN_ENTRIES:
                winners_log.pop(0)
            update_winners_log(winners_log)
            
        except Exception as e:
            print(f"❌ Error during 2nd place reply: {e}")

print("🤖 Bot is armed and running. Entering hourly loop...")

while True:
    try:
        run_hourly_competition()
    except Exception as e:
        print(f"🚨 Unhandled loop exception (preventing crash): {e}")
    
    print("💤 Sleeping for 1 hour...")
    time.sleep(3600)
