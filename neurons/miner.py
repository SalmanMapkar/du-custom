# The MIT License (MIT)
# Copyright © 2023 Data Universe

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
# import sys
# sys.path.append("/mnt/d/Google Drive/Temp/Desktop/Freelance/Clients/Potiential Clients/Varun S/code/du-custom")
import asyncio
from collections import defaultdict
import copy
import sys
import threading
import time
import traceback
import typing
import bittensor as bt
import datetime as dt
from common import constants, utils
from common.data import CompressedMinerIndex
from common.protocol import GetDataEntityBucket, GetMinerIndex
from neurons.config import NeuronType
from scraping.config.config_reader import ConfigReader
from scraping.coordinator import ScraperCoordinator
from scraping.provider import ScraperProvider
from storage.miner.sqlite_miner_storage import SqliteMinerStorage
from neurons.config import NeuronType, check_config, create_config


class Miner:
    """The Glorious Miner."""

    def __init__(self, config=None):
        self.config = copy.deepcopy(config or create_config(NeuronType.MINER))
        check_config(self.config)

        bt.logging(config=self.config, logging_dir=self.config.full_path)
        bt.logging.info(self.config)

        if self.config.offline:
            bt.logging.success(
                "Running in offline mode. Skipping bittensor object setup and axon creation."
            )
        else:
            # The wallet holds the cryptographic key pairs for the miner.
            self.wallet = bt.wallet(config=self.config)
            bt.logging.info(f"Wallet: {self.wallet}.")

            # The subtensor is our connection to the Bittensor blockchain.
            self.subtensor = bt.subtensor(config=self.config)
            bt.logging.info(f"Subtensor: {self.subtensor}.")

            # The metagraph holds the state of the network, letting us know about other validators and miners.
            self.metagraph = self.subtensor.metagraph(self.config.netuid)
            bt.logging.info(f"Metagraph: {self.metagraph}.")

            # Each miner gets a unique identity (UID) in the network for differentiation.
            # TODO: Stop doing meaningful work in the constructor to make neurons more testable.
            if self.wallet.hotkey.ss58_address in self.metagraph.hotkeys:
                self.uid = self.metagraph.hotkeys.index(self.wallet.hotkey.ss58_address)
                bt.logging.info(
                    f"Running neuron on subnet: {self.config.netuid} with uid {self.uid} using network: {self.subtensor.chain_endpoint}."
                )
            else:
                self.uid = 0
                bt.logging.warning(
                    f"Hotkey {self.wallet.hotkey.ss58_address} not found in metagraph. Assuming this is a test."
                )

            self.last_sync_timestamp = dt.datetime.min
            self.step = 0

            # The axon handles request processing, allowing validators to send this miner requests.
            self.axon = bt.axon(wallet=self.wallet, port=self.config.axon.port)

            # Attach determiners which functions are called when servicing a request.
            bt.logging.info("Attaching forward function to miner axon.")
            self.axon.attach(
                forward_fn=self.get_index,
                blacklist_fn=self.get_index_blacklist,
                priority_fn=self.get_index_priority,
            ).attach(
                forward_fn=self.get_data_entity_bucket,
                blacklist_fn=self.get_data_entity_bucket_blacklist,
                priority_fn=self.get_data_entity_bucket_priority,
            )
            bt.logging.success(f"Axon created: {self.axon}.")

        # Instantiate runners.
        self.should_exit: bool = False
        self.is_running: bool = False
        self.thread: threading.Thread = None
        self.compressed_index_refresh_thread: threading.Thread = None
        self.lock = threading.RLock()

        # Instantiate storage.
        self.storage = SqliteMinerStorage(
            self.config.neuron.database_name,
            self.config.neuron.max_database_size_gb_hint,
        )

        bt.logging.success(
            f"Successfully connected to miner storage: {self.config.neuron.database_name}."
        )

        # Configure the ScraperCoordinator
        bt.logging.info(
            f"Loading scraping config from {self.config.neuron.scraping_config_file}."
        )
        scraping_config = ConfigReader.load_config(
            self.config.neuron.scraping_config_file
        )
        bt.logging.success(f"Loaded scraping config: {scraping_config}.")

        self.scraping_coordinator = ScraperCoordinator(
            scraper_provider=ScraperProvider(),
            miner_storage=self.storage,
            config=scraping_config,
        )

        # Configure per hotkey request limits.
        self.request_lock = threading.RLock()
        self.last_cleared_request_limits = dt.datetime.now()
        self.requests_by_hotkey = defaultdict(lambda: 0)

    def refresh_index(self):
        """
        Refreshes the cached compressed miner index periodically off the hot path of GetMinerIndex requests.
        """
        while not self.should_exit:
            try:
                # Refresh the index if it hasn't been refreshed in the configured time period.
                self.storage.refresh_compressed_index(
                    time_delta=constants.MINER_CACHE_FRESHNESS
                )
                bt.logging.trace("Refresh index thread finished refreshing the index.")
                # Wait freshness period + 1 minute to try refreshing again.
                # Wait the additional minute to ensure that the next refresh sees a 'stale' index.
                time.sleep(
                    (
                        constants.MINER_CACHE_FRESHNESS + dt.timedelta(minutes=1)
                    ).total_seconds()
                )
            # In case of unforeseen errors, the refresh thread will log the error and continue operations.
            except Exception:
                bt.logging.error(traceback.format_exc())
                # Sleep 5 minutes to avoid constant refresh attempts if they are consistently erroring.
                time.sleep(60 * 5)

    def run(self):
        """
        Initiates and manages the main loop for the miner.
        """

        if self.config.offline:
            bt.logging.success("Running in offline mode. Skipping axon serving.")
        else:
            # Check that miner is registered on the network.
            self.sync()

            # Serve passes the axon information to the network + netuid we are hosting on.
            # This will auto-update if the axon port of external ip have changed.
            bt.logging.info(
                f"Serving miner axon {self.axon} on network: {self.config.subtensor.chain_endpoint} with netuid: {self.config.netuid}."
            )
            self.axon.serve(netuid=self.config.netuid, subtensor=self.subtensor)

            # Start  starts the miner's axon, making it active on the network.
            self.axon.start()

            self.last_sync_timestamp = dt.datetime.now()
            bt.logging.success(f"Miner starting at {self.last_sync_timestamp}.")

        self.scraping_coordinator.run_in_background_thread()

        # This loop maintains the miner's operations until intentionally stopped.
        try:
            # In offline mode we just idle while the scraping_coordinator runs.
            if self.config.offline:
                while not self.should_exit:
                    time.sleep(12)
            else:
                while not self.should_exit:
                    # Epoch length defaults to 100 blocks at 12 seconds each for 20 minutes.
                    while dt.datetime.now() - self.last_sync_timestamp < (
                        dt.timedelta(seconds=12 * self.config.neuron.epoch_length)
                    ):
                        # Wait before checking again.
                        time.sleep(12)

                        # Check if we should exit.
                        if self.should_exit:
                            break

                    # Sync metagraph.
                    self.sync()

                    self._log_status(self.step)

                    self.last_sync_timestamp = dt.datetime.now()
                    self.step += 1

        # If someone intentionally stops the miner, it'll safely terminate operations.
        except KeyboardInterrupt:
            if not self.config.offline:
                self.axon.stop()
            self.scraping_coordinator.stop()
            bt.logging.success("Miner killed by keyboard interrupt.")
            sys.exit()

        # In case of unforeseen errors, the miner will log the error and continue operations.
        except Exception as e:
            bt.logging.error(traceback.format_exc())

    def run_in_background_thread(self):
        """
        Starts the miner's operations in a separate background thread.
        This is useful for non-blocking operations.
        """
        if not self.is_running:
            bt.logging.debug("Starting miner in background thread.")
            self.should_exit = False
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            self.compressed_index_refresh_thread = threading.Thread(
                target=self.refresh_index, daemon=True
            )
            self.compressed_index_refresh_thread.start()
            self.is_running = True
            bt.logging.debug("Started")

    def stop_run_thread(self):
        """
        Stops the miner's operations that are running in the background thread.
        """
        if self.is_running:
            bt.logging.debug("Stopping miner in background thread.")
            self.should_exit = True
            self.thread.join(5)
            self.compressed_index_refresh_thread.join(5)
            self.is_running = False
            bt.logging.debug("Stopped")

    def __enter__(self):
        """
        Starts the miner's operations in a background thread upon entering the context.
        This method facilitates the use of the miner in a 'with' statement.
        """
        self.run_in_background_thread()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Stops the miner's background operations upon exiting the context.
        This method facilitates the use of the miner in a 'with' statement.

        Args:
            exc_type: The type of the exception that caused the context to be exited.
                      None if the context was exited without an exception.
            exc_value: The instance of the exception that caused the context to be exited.
                       None if the context was exited without an exception.
            traceback: A traceback object encoding the stack trace.
                       None if the context was exited without an exception.
        """
        self.stop_run_thread()

    def resync_metagraph(self):
        """Resyncs the metagraph and updates the hotkeys and moving averages based on the new metagraph."""
        bt.logging.info("Attempting to resync the metagraph.")

        # Sync the metagraph.
        new_metagraph = self.subtensor.metagraph(netuid=self.config.netuid)
        with self.lock:
            self.metagraph = new_metagraph

        bt.logging.success("Successfuly resynced the metagraph.")

    def _log_status(self, step: int):
        """Logs a summary of the miner status in the subnet."""
        relative_incentive = self.metagraph.I[self.uid].item() / max(self.metagraph.I)
        incentive_and_hk = zip(self.metagraph.I, self.metagraph.hotkeys)
        incentive_and_hk = sorted(incentive_and_hk, key=lambda x: x[0], reverse=True)
        position = -1
        for i, (_, hk) in enumerate(incentive_and_hk):
            if hk == self.wallet.hotkey.ss58_address:
                position = i
                break
        log = (
            f"Step:{step} | "
            f"Block:{self.metagraph.block.item()} | "
            f"Stake:{self.metagraph.S[self.uid]} | "
            f"Incentive:{self.metagraph.I[self.uid]} | "
            f"Relative Incentive:{relative_incentive} | "
            f"Position:{position} | "
            f"Emission:{self.metagraph.E[self.uid]}"
        )
        bt.logging.info(log)

    async def get_index(self, synapse: GetMinerIndex) -> GetMinerIndex:
        """Runs after the GetMinerIndex synapse has been deserialized (i.e. after synapse.data is available)."""
        bt.logging.info(
            f"Got to a GetMinerIndex request from {synapse.dendrite.hotkey}."
        )

        if synapse.version and synapse.version >= 2:
            # List all the data entity buckets that this miner currently has.
            compressed_index = None
            # Return the appropriate amount of max buckets based on protocol of the requesting validator.
            if synapse.version == 4:
                compressed_index = self.storage.get_compressed_index(
                    bucket_count_limit=constants.DATA_ENTITY_BUCKET_COUNT_LIMIT_PER_MINER_INDEX_PROTOCOL_4
                )
            # Only synapse.version 4 is supported at this time.
            else:
                bt.logging.error(f"Unsupported protocol version: {synapse.version}.")

            synapse.compressed_index_serialized = compressed_index.json()
            bt.logging.success(
                f"Returning compressed miner index of {CompressedMinerIndex.size_bytes(compressed_index)} bytes "
                + f"across {CompressedMinerIndex.bucket_count(compressed_index)} buckets to {synapse.dendrite.hotkey}."
            )
        else:
            synapse.data_entity_buckets = self.storage.list_data_entity_buckets()

            # Calculate total size of returned index for logging.
            size = 0
            for bucket in synapse.data_entity_buckets:
                size += bucket.size_bytes

            bt.logging.success(
                f"Returning uncompressed miner index of {size} bytes across {len(synapse.data_entity_buckets)} buckets "
                + f"to {synapse.dendrite.hotkey}."
            )

        synapse.version = constants.PROTOCOL_VERSION

        return synapse

    async def get_index_blacklist(
        self, synapse: GetMinerIndex
    ) -> typing.Tuple[bool, str]:
        return self.default_blacklist(synapse)

    async def get_index_priority(self, synapse: GetMinerIndex) -> float:
        return self.default_priority(synapse)

    async def get_data_entity_bucket(
        self, synapse: GetDataEntityBucket
    ) -> GetDataEntityBucket:
        """Runs after the GetDataEntityBucket synapse has been deserialized (i.e. after synapse.data is available)."""
        bt.logging.info(
            f"Got to a GetDataEntityBucket request from {synapse.dendrite.hotkey} for Bucket ID: {str(synapse.data_entity_bucket_id)}."
        )

        # List all the data entities that this miner has for the requested DataEntityBucket.
        synapse.data_entities = self.storage.list_data_entities_in_data_entity_bucket(
            synapse.data_entity_bucket_id
        )
        synapse.version = constants.PROTOCOL_VERSION

        bt.logging.success(
            f"Returning Bucket ID: {str(synapse.data_entity_bucket_id)} with {len(synapse.data_entities)} entities to {synapse.dendrite.hotkey}."
        )

        return synapse

    async def get_data_entity_bucket_blacklist(
        self, synapse: GetDataEntityBucket
    ) -> typing.Tuple[bool, str]:
        return self.default_blacklist(synapse)

    async def get_data_entity_bucket_priority(
        self, synapse: GetDataEntityBucket
    ) -> float:
        return self.default_priority(synapse)

    def default_blacklist(self, synapse: bt.Synapse) -> typing.Tuple[bool, str]:
        """The default blacklist that only allows requests from validators."""
        if synapse.dendrite.hotkey in [
            "5Gpt8XWFTXmKrRF1qaxcBQLvnPLpKi6Pt2XC4vVQR7gqNKtU"
        ]:
            return (
                True,
                f"Explictly blacklisted hotkey {synapse.dendrite.hotkey} at {synapse.dendrite.ip}",
            )

        if synapse.dendrite.hotkey not in self.metagraph.hotkeys:
            # Ignore requests from unrecognized entities.
            return (
                True,
                f"Unrecognized hotkey {synapse.dendrite.hotkey} at {synapse.dendrite.ip}",
            )

        uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
        if not utils.is_validator(uid, self.metagraph):
            return (
                True,
                f"Hotkey {synapse.dendrite.hotkey} at {synapse.dendrite.ip} is not a validator",
            )

        # TODO: Break out limit per API.
        with self.request_lock:
            # Check if we need to clear the request limit counter.
            if (
                dt.datetime.now() - self.last_cleared_request_limits
            ) >= constants.MIN_EVALUATION_PERIOD:
                bt.logging.trace(
                    f"Clearing request limit counter by hotkey after an eval period: {constants.MIN_EVALUATION_PERIOD}."
                )
                self.requests_by_hotkey = defaultdict(lambda: 0)
                self.last_cleared_request_limits = dt.datetime.now()

            # Record request.
            self.requests_by_hotkey[synapse.dendrite.hotkey] += 1

            # Blacklist if over request limit.
            # We allow up to 4 requests in case a validator restarts and sends two pairs of index/bucket requests.
            if self.requests_by_hotkey[synapse.dendrite.hotkey] > 4:
                return (
                    True,
                    f"Hotkey {synapse.dendrite.hotkey} at {synapse.dendrite.ip} over eval period request limit",
                )

        return False, ""

    def default_priority(self, synapse: bt.Synapse) -> float:
        """The default priority that prioritizes by validator stake."""
        caller_uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
        priority = float(self.metagraph.S[caller_uid])
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: {priority}.",
        )
        return priority

    def get_config_for_test(self) -> bt.config:
        return self.config

    def sync(self):
        """
        Wrapper for synchronizing the state of the network for the given miner.
        """
        # Ensure miner hotkey is still registered on the network.
        self.check_registered()

        self.resync_metagraph()

    def check_registered(self):
        # --- Check for registration.
        if not self.subtensor.is_hotkey_registered(
            netuid=self.config.netuid,
            hotkey_ss58=self.wallet.hotkey.ss58_address,
        ):
            bt.logging.error(
                f"Wallet: {self.wallet} is not registered on netuid {self.config.netuid}."
                f" Please register the hotkey using `btcli subnets register` before trying again."
            )
            sys.exit(1)


# This is the main function, which runs the miner.
if __name__ == "__main__":
    with Miner() as miner:
        while True:
            time.sleep(60)
