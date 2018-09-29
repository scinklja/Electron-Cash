import copy
import datetime
import time
from functools import partial
import json
import threading
import sys
from os.path import basename, splitext

from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *

from electroncash.address import Address, PublicKey, Base58Error
from electroncash.bitcoin import base_encode, TYPE_ADDRESS, TYPE_SCRIPT
from electroncash.i18n import _
from electroncash.plugins import run_hook

from .util import *

from electroncash.util import bfh, format_satoshis_nofloat, format_satoshis_plain_nofloat, NotEnoughFunds, ExcessiveFee
from electroncash.transaction import Transaction

from electroncash import bitcoinfiles

from .transaction_dialog import show_transaction

from electroncash.bitcoinfiles import *

dialogs = []  # Otherwise python randomly garbage collects the dialogs...


class BitcoinFilesUploadDialog(QDialog, MessageBoxMixin):

    def __init__(self, parent, file_receiver=None, show_on_create=False):
        # We want to be a top-level window
        QDialog.__init__(self, parent)

        # check parent window type
        self.parent = parent
        from .slp_create_token_genesis_dialog import SlpCreateTokenGenesisDialog
        from .main_window import ElectrumWindow
        if isinstance(parent, SlpCreateTokenGenesisDialog):
            self.main_window = parent.main_window
            self.network = parent.main_window.network
        elif isinstance(parent, ElectrumWindow):
            self.main_window = parent
            self.network = parent.network
        else:
            raise Exception("Parent must be of type ElectrumWindow or SlpCreateTokenGenesisDialog")

        self.file_receiver = file_receiver
        self.metadata = None
        self.filename = None
        self.is_dirty = False

        self.setWindowTitle(_("Upload Token Document"))

        vbox = QVBoxLayout()
        self.setLayout(vbox)

        vbox.addWidget(QLabel("Upload and download documents using the Bitcoin Files Protocol (<a href=https://bitcoinfiles.com>bitcoinfiles.com</a>)"))

        # Select File
        self.select_file_button = b = QPushButton(_("Select File..."))
        b.setAutoDefault(True)
        b.setDefault(True)
        b.clicked.connect(self.select_file)
        vbox.addWidget(self.select_file_button)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        vbox.addLayout(grid)
        row = 0

        # Local file path
        grid.addWidget(QLabel(_('Local Path:')), row, 0)
        self.path = QLineEdit("")
        self.path.setReadOnly(True)
        self.path.setFixedWidth(570)
        grid.addWidget(self.path, row, 1)
        row += 1

        # Estimated Fees
        grid.addWidget(QLabel(_('Upload Cost (satoshis):')), row, 0)
        self.upload_cost_label = QLabel("")
        grid.addWidget(self.upload_cost_label, row, 1)
        row += 1

        # File hash
        grid.addWidget(QLabel(_('File sha256 (auto-populated):')), row, 0)
        self.hash = QLineEdit("")
        self.hash.setReadOnly(True)
        self.hash.setFixedWidth(570)
        self.hash.setInputMask("HHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHH")
        grid.addWidget(self.hash, row, 1)
        row += 1

        # Previous file hash
        grid.addWidget(QLabel(_('Previous file sha256 (manual entry):')), row, 0)
        self.prev_hash = QLineEdit("")
        self.prev_hash.setReadOnly(False)
        self.prev_hash.setFixedWidth(570)
        self.prev_hash.setInputMask("HHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHH")
        self.prev_hash.textChanged.connect(self.make_dirty)
        grid.addWidget(self.prev_hash, row, 1)
        row += 1

        # File Receiver Checkbox
        self.token_fixed_supply_cb = cb = QCheckBox(_('Send file to a BCH address'))
        self.token_fixed_supply_cb.setChecked(False)
        grid.addWidget(self.token_fixed_supply_cb, row, 1)
        cb.clicked.connect(self.toggle_receiver_addr)
        row += 1

        # Specific file receiver
        self.file_receiver_label = QLabel(_('Receiver Address:'))
        self.file_receiver_label.setHidden(True)
        grid.addWidget(self.file_receiver_label, row, 0)
        self.file_receiver_e = QLineEdit("")
        self.file_receiver_e.setHidden(True)
        self.file_receiver_e.setReadOnly(False)
        self.file_receiver_e.setFixedWidth(570)
        self.file_receiver_e.textChanged.connect(self.make_dirty)
        grid.addWidget(self.file_receiver_e, row, 1)
        row += 1

        # File path
        grid.addWidget(QLabel(_('URI after upload (auto-populated):')), row, 0)
        self.bitcoinfileAddr_label = QLineEdit("")
        self.bitcoinfileAddr_label.setReadOnly(True)
        self.bitcoinfileAddr_label.setFixedWidth(570)
        grid.addWidget(self.bitcoinfileAddr_label, row, 1)

        self.progress_label = QLabel("")
        vbox.addWidget(self.progress_label)

        self.progress = QProgressBar(self)
        self.progress.setGeometry(200, 80, 250, 20)
        vbox.addWidget(self.progress)

        hbox = QHBoxLayout()
        vbox.addLayout(hbox)

        self.cancel_button = b = QPushButton(_("Cancel"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setDefault(False)
        b.clicked.connect(self.close)
        b.setDefault(False)
        hbox.addWidget(self.cancel_button)

        hbox.addStretch(1)

        self.sign_button = b = QPushButton(_("Sign"))
        self.sign_button.setAutoDefault(False)
        self.sign_button.setDefault(False)
        self.sign_button.setDisabled(True)
        b.clicked.connect(self.sign_txns)
        b.setDefault(False)
        hbox.addWidget(self.sign_button)

        self.upload_button = b = QPushButton(_("Upload"))
        self.upload_button.setAutoDefault(False)
        self.upload_button.setDefault(False)
        self.upload_button.setDisabled(True)
        b.clicked.connect(self.upload)
        b.setDefault(False)
        hbox.addWidget(self.upload_button)

        if show_on_create:
            self.setModal(True)
            self.show()

    def toggle_receiver_addr(self):
        self.file_receiver_e.setVisible(not self.file_receiver_e.isVisible())
        self.file_receiver_label.setVisible(not self.file_receiver_label.isVisible())
        if not self.file_receiver_e.isVisible():
            self.file_receiver_e.setText('')

    def make_dirty(self):
        self.is_dirty = True
        self.upload_button.setDisabled(True)
        self.progress.setValue(0)
        self.tx_batch = []
        self.tx_batch_signed_count = 0
        self.chunks_processed = 0
        self.chunks_total = 0
        self.final_metadata_txn_created = False
        if self.filename != '':
            self.sign_button.setEnabled(True)
            self.sign_button.setDefault(True)
        else:
            self.select_file_button.setDefault(True)

    def sign_txns(self):

        # set all file Metadata to None for now... UI needs updated for this
        self.metadata = { 'filename': None, 'fileext': None, 'filesize': None, 'file_sha256': None, 'prev_file_sha256': None, 'uri': None }
        self.metadata['prev_file_sha256'] = self.prev_hash.text()

        if self.prev_hash.text() != '':
            if len(self.prev_hash.text()) != 64:
                self.show_message(_("Previous document hash must be a 32 byte hexidecimal string or left empty."))
                return

        if self.file_receiver_e.text() != '':
            try: 
                addr = Address.from_string(self.file_receiver_e.text())
            except Base58Error:
                self.show_message(_("Receiver address checksum fails."))
                return
            
            self.file_receiver = Address.from_string(self.file_receiver_e.text())
        else:
            self.file_receiver = None
    
        if self.filename != '':
            self.select_file_button.setDefault(False)
            with open(self.filename,"rb") as f:

                # clear fields before re-populating
                self.hash.setText('')
                self.path.setText('')
                self.upload_cost_label.setText('')
                self.bitcoinfileAddr_label.setText('')

                bytes = f.read()
                if len(bytes) > 5261:
                    self.show_error("Files cannot be larger than 5.261kB in size.")
                    return
                import hashlib
                readable_hash = hashlib.sha256(bytes).hexdigest()
                self.hash.setText(readable_hash)
                self.path.setText(self.filename)
                self.metadata['filesize'] = len(bytes)
                try:
                    self.metadata['filename'] = splitext(basename(self.filename))[0]
                    self.metadata['fileext'] = splitext(basename(self.filename))[1]
                except IndexError:
                    pass
                self.metadata['file_sha256'] = readable_hash
                cost = calculateUploadCost(len(bytes), self.metadata)
                self.upload_cost_label.setText(str(cost))
                addr = self.parent.wallet.get_unused_address()

                # IMPORTANT: set wallet.send_slpTokenId to None to guard tokens during this transaction
                self.main_window.token_type_combo.setCurrentIndex(0)
                assert self.main_window.wallet.send_slpTokenId == None

                try:
                    self.tx_batch.append(getFundingTxn(self.parent.wallet, addr, cost, self.parent.config))
                except NotEnoughFunds:
                    self.show_message("Insufficient funds.\n\nYou must have at least balance of at least: " + str(cost) + " sat AND have at least 1 block confirmation.")
                    return

                # Rewind and put file into chunks
                f.seek(0, 0)
                chunks = []
                while True:
                    b = f.read(220)
                    if b == b'': break
                    try:
                        chunks.append(b)
                        self.chunks_total += 1
                    except ValueError:
                        break

                min_len = 223 - len(make_bitcoinfile_metadata_opreturn(1, 0, None, self.metadata['filename'], self.metadata['fileext'], self.metadata['filesize'], self.metadata['file_sha256'], self.metadata['prev_file_sha256'], self.metadata['uri'])[1].to_script())
                
                # determine if the metadata txn data chunk will be empty for progress bar accuracy
                if len(bytes) < 220:
                    chunk_count_adder = 1 if len(bytes) > min_len else 0
                else:
                    chunk_count_adder = 1 if min_len - (len(bytes) % 220) < 0 else 0

                self.progress.setMaximum(len(chunks) + chunk_count_adder + 1)
                self.progress.setMinimum(0)
                self.progress_label.setText("Signing 1 of " + str(len(chunks) + chunk_count_adder + 1) + " transactions")
                
                # callback to recursive sign next txn or finish
                def sign_done(success):
                    if success:
                        self.tx_batch_signed_count += 1
                        self.progress.setValue(self.tx_batch_signed_count)
                        self.activateWindow()
                        self.raise_()
                        self.progress_label.setText("Signing " + str(self.tx_batch_signed_count + 1) + " of " + str(len(chunks) + chunk_count_adder + 1) + " transactions")
                        if self.chunks_processed <= self.chunks_total and not self.final_metadata_txn_created:
                            try:
                                chunk_bytes = chunks[self.chunks_processed]
                            except IndexError:
                                chunk_bytes = None
                            try:
                                txn, self.final_metadata_txn_created = getUploadTxn(self.parent.wallet, self.tx_batch[self.chunks_processed], self.chunks_processed, self.chunks_total, chunk_bytes, self.parent.config, self.metadata, self.file_receiver)
                                self.tx_batch.append(txn)
                            except NotEnoughFunds as e:
                                self.show_message("Insufficient funds for file chunk #" + str(self.chunks_processed + 1))
                                return
                            self.chunks_processed += 1

                        if self.tx_batch_signed_count < len(self.tx_batch):
                            self.main_window.sign_tx(self.tx_batch[self.tx_batch_signed_count], sign_done)
                        else:
                            uri = "bitcoinfile:" + self.tx_batch[len(self.tx_batch)-1].txid()
                            self.bitcoinfileAddr_label.setText(uri)
                            self.progress_label.setText("Signing complete. Ready to upload.")
                            self.is_dirty = False
                            self.progress.setValue(0)
                            self.sign_button.setDisabled(True)
                            self.upload_button.setEnabled(True)
                            self.upload_button.setDefault(True)
                            self.activateWindow()
                            self.raise_()
                self.main_window.sign_tx(self.tx_batch[0], sign_done)

    def select_file(self):
        self.progress.setValue(0)
        self.tx_batch = []
        self.tx_batch_signed_count = 0
        self.chunks_processed = 0
        self.chunks_total = 0
        self.final_metadata_txn_created = False
        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        self.filename, _ = QFileDialog.getOpenFileName(self, "Select File to Upload", "","All Files (*)", options=options)
        self.sign_txns()
        
    def upload(self):
        if not self.is_dirty:
            self.progress_label.setText("Broadcasting 1 of " + str(len(self.tx_batch)) + " transactions")
            self.progress.setMinimum(0)
            self.progress.setMaximum(len(self.tx_batch))
            broadcast_count = 0
            # Broadcast all transaction to the nexwork
            for tx in self.tx_batch:
                tx_desc = None
                status, msg = self.network.broadcast(tx)
                # print(status)
                # print(msg)
                if status == False:
                    self.show_error(msg)
                    self.show_error("Upload failed. Try again.")
                    return

                broadcast_count += 1
                time.sleep(0.1)
                self.progress_label.setText("Broadcasting " + str(broadcast_count) + " of " + str(len(self.tx_batch)) + " transactions")
                self.progress.setValue(broadcast_count)
                QApplication.processEvents()

            try:
                self.parent.token_dochash_e.setText(self.hash.text())
                self.parent.token_url_e.setText(self.bitcoinfileAddr_label.text())
            except AttributeError:
                pass

            self.show_message("File upload complete.")
            self.close()

    def closeEvent(self, event):
        event.accept()
        self.parent.raise_()
        self.parent.activateWindow()
        try:
            dialogs.remove(self)
        except ValueError:
            pass