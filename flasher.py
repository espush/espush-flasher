#!/usr/bin/env python
# encoding: utf-8

import os
import sys
import time
import serial
import struct
import hashlib
import requests
from serial.tools.list_ports import comports

from PyQt4 import QtNetwork
from PyQt4.QtGui import QWidget, QApplication, QMessageBox, QMenu, QCursor
from PyQt4.QtNetwork import QNetworkRequest, QNetworkReply
from PyQt4.QtCore import QVariant, QUrl, QObject, QThread, pyqtSignal, QByteArray, QFile, QIODevice

from etool import ESPROM, div_roundup

import dlg


ROMS_DIR = "down_roms"
FLASH_BAUD = 576000


class ESP8266Flasher(QObject):
    begin_flash_sig = pyqtSignal(str, dict)
    abort_flash_sig = pyqtSignal()
    conn_result_sig = pyqtSignal(int)
    flash_progress_sig = pyqtSignal(int, int)
    flash_result_sig = pyqtSignal(int, str)
    console_sig = pyqtSignal(str)

    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        self._is_abort = False

    def sync_dev(self):
        try:
            self.consolelog(u'串口同步中，请勿终止')
            self.esp8266.connect()
            return True
        except Exception as e:
            print(e)
            return False

    def consolelog(self, s):
        self.console_sig.emit(s)

    def _flash_write(self, comport, firmwares):
        sync_result = self.sync_dev()
        QApplication.processEvents()
        print('IS_ABORT: %d %d' % (self._is_abort, sync_result))
        self.conn_result_sig.emit(0 if sync_result else 1)
        # 同步设备失败
        if not sync_result:
            self.esp8266.close()
            return
        # 检查是否要终止烧录
        if self._is_abort:
            self.flash_result_sig.emit(1, u'已终止')
            self.esp8266.close()
            return
        flash_info = self.make_flash_info()
        total_size = len(firmwares['boot']) + len(firmwares['app1']) + 3 * len(firmwares['blank']) + len(firmwares['init'])
        self.flash_write(flash_info, 0, firmwares['boot'], total_size)
        self.flash_write(flash_info, 0x1000, firmwares['app1'], total_size)
        self.flash_write(flash_info, 0xc8000, firmwares['blank'], total_size)
        self.flash_write(flash_info, 0x3fb000, firmwares['blank'], total_size)
        self.flash_write(flash_info, 0x3fc000, firmwares['init'], total_size)
        self.flash_write(flash_info, 0x3fe000, firmwares['blank'], total_size)
        self.flash_result_sig.emit(0, u'成功')

    def begin_flash(self, comport, firmwares):
        self._is_abort = False
        port = str(comport.toUtf8())
        try:
            self.esp8266 = ESPROM(port, FLASH_BAUD)
        except serial.serialutil.SerialException as _:
            self.consolelog(u'串口读写失败，请检查是否有其他程序占用了指定串口')
            self.flash_result_sig.emit(1, u'串口读写失败')
            return
        self._flash_write(comport, firmwares)
        self.esp8266.close()

    def make_flash_info(self):
        flash_mode = {'qio': 0, 'qout': 1, 'dio': 2, 'dout': 3}['dio']
        flash_size_freq = {'4m': 0x00, '2m': 0x10, '8m': 0x20, '16m': 0x30, '32m': 0x40, '16m-c1': 0x50, '32m-c1': 0x60, '32m-c2': 0x70}['32m-c1']
        flash_size_freq += {'40m': 0, '26m': 1, '20m': 2, '80m': 0xf}['40m']
        return struct.pack('BB', flash_mode, flash_size_freq)

    def flash_write(self, flash_info, address, content, total_size):
        image = str(content)
        print('write flash %d' % len(image))
        self.consolelog(u'Flash擦除工作进行中，请保持设备连接。')
        blocks = div_roundup(len(content), self.esp8266.ESP_FLASH_BLOCK)
        self.esp8266.flash_begin(blocks * self.esp8266.ESP_FLASH_BLOCK, address)
        seq = 0
        written = 0
        t = time.time()
        while len(image) > 0:
            QApplication.processEvents()
            if self._is_abort:
                self.flash_result_sig.emit(1, u'已终止')
                self.esp8266.close()
                return
            # print('\rWriting at 0x%08x... (%d %%)' % (address + seq * self.esp8266.ESP_FLASH_BLOCK, 100 * (seq + 1) / blocks),)
            sys.stdout.flush()
            block = image[0: self.esp8266.ESP_FLASH_BLOCK]
            actual_written = len(block)
            # Fix sflash config data
            if address == 0 and seq == 0 and block[0] == b'\xe9':
                block = block[0:2] + flash_info + block[4:]
            # Pad the last block
            block = block + b'\xff' * (self.esp8266.ESP_FLASH_BLOCK - len(block))
            self.esp8266.flash_block(block, seq)
            image = image[self.esp8266.ESP_FLASH_BLOCK:]
            seq += 1
            written += len(block)
            self.flash_progress_sig.emit(actual_written, total_size)
        t = time.time() - t
        print('\rWrote %d bytes at 0x%08x in %.1f seconds (%.1f kbit/s)...' % (written, address, t, written / t * 8 / 1000))

    def abort_flash(self):
        print('abort flash.')
        self._is_abort = True


class FlashDlg(QWidget):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.ui = dlg.Ui_Form()
        self.ui.setupUi(self)
        self.ui_init()
        self.init_comports()
        self.init_romlist()
        # menu
        self.ctx_menu_init()
        # or ABORT
        self.init_btn()
        # flasher thread
        self.init_flasher_thread()

    def ctx_menu_init(self):
        self.pop_menu = QMenu()
        self.pop_menu.addAction(self.ui.action_clear_console)

    def init_btn(self):
        self.change_btn_to_flash()

    def change_btn_to_flash(self):
        self.btn_state = 'FLASH'
        self.ui.gobtn.setText(u'开始')
        # 已写入字节数
        self.written = 0
        # 时间消耗
        self.elapse = time.time()

    def change_btn_to_abort(self):
        self.btn_state = 'ABORT'
        self.ui.gobtn.setText(u'终止')

    def action_state(self):
        return self.btn_state

    def closeEvent(self, evt):
        """
        :type evt: QCloseEvent
        :param evt:
        """
        self.flash_thread.exit()
        evt.accept()

    def init_flasher_thread(self):
        self.flasher = ESP8266Flasher()
        self.flash_thread = QThread()
        self.flasher.moveToThread(self.flash_thread)
        self.flash_thread.start()
        '''
            begin_flash_sig = pyqtSignal(str, dict)
            abort_flash_sig = pyqtSignal()
            conn_result_sig = pyqtSignal(int)
            flash_progress_sig = pyqtSignal(int)
            flash_result_sig = pyqtSignal(int)
            console_sig = pyqtSignal(str)
        '''
        self.flasher.begin_flash_sig.connect(self.flasher.begin_flash)
        self.flasher.abort_flash_sig.connect(self.flasher.abort_flash)
        self.flasher.conn_result_sig.connect(self.conn_result)
        self.flasher.flash_progress_sig.connect(self.flash_progress)
        self.flasher.flash_result_sig.connect(self.flash_result)
        self.flasher.console_sig.connect(self.consolelog)

    def conn_result(self, res):
        print('connect result is %r' % res)
        if res == 0:
            self.consolelog(u'串口同步成功，烧录即将进行')
        if res == 1:
            self.consolelog(u'同步串口失败，请检查所选串口并重试')
            self.change_btn_to_flash()

    def flash_result(self, res, desc):
        print('flash result is %r' % res)
        elapse = time.time() - self.elapse
        if res == 1:
            self.consolelog(u'烧录失败 %s 耗时 %d 秒' % (desc, elapse))
        if res == 0:
            self.consolelog(u'固件烧录成功, 耗时 %d 秒' % elapse)
        self.change_btn_to_flash()

    def flash_progress(self, res, total):
        print('flash progress is %d, total %d, writed %d' % (res, total, self.written))
        self.written += res
        self.ui.progbar.setValue( ( float(self.written) / total) * 100 )

    def ui_init(self):
        self.setFixedSize(self.size())
        self.consolelog(u'ESPUSH一键烧录工具 v2.0')
        url = u'https://espush.cn/api/portal/qqgroup'
        self.consolelog(u'加入 <a href="%s">ESPush IoT QQ群</a> 进行讨论。' % url)
        self.consolelog(u"\n")
        self.ui.textOut.setOpenLinks(True)
        self.ui.textOut.setOpenExternalLinks(True)

    def init_romlist(self):
        url = 'https://espush.cn/api/portal/admin/flasher/firmwares'
        rsp = requests.get(url)
        data = rsp.json()
        for firm in data:
            v = QVariant((firm, ))
            self.ui.firm_box.addItem(firm['description'], userData=v)

    def init_comports(self):
        ports = comports()
        self.ui.com_box.clear()
        ports = [el.device for el in ports]
        for port in ports:
            self.ui.com_box.addItem(port)

    def consolelog(self, msg):
        self.ui.textOut.append(msg)

    def console_clear(self):
        self.ui.textOut.clear()

    def down_firmfile(self, firm):
        url = 'https://espush.cn/api/portal/admin/down/firmwares/%d' % firm['id']
        rsp = requests.get(url)
        if rsp.status_code != 200:
            self.consolelog(u'下载失败')
            return
        if not self.checksum(rsp.content, firm['checksum']):
            self.consolelog('checksum failed!')
            return
        # write to file.
        self.write_local_firm_file(firm['id'], rsp.content)
        return rsp.content

    def write_local_firm_file(self, fid, content):
        # 文件夹是否存在，不存在则新增
        if not os.path.exists(ROMS_DIR):
            try:
                os.mkdir(ROMS_DIR)
            except IOError:
                self.consolelog(u'创建下载 ROM 临时文件夹失败')
                return
        # 写入文件
        self.consolelog(u'下载完毕，写入本地缓存文件')
        with open('%s/%d' % (ROMS_DIR, fid), 'wb') as fout:
            fout.write(content)

    def checksum(self, content, csum):
        md5 = lambda x: hashlib.md5(x).hexdigest()
        return md5(content) == csum

    def local_firm_exist(self, firm):
        firmname = '%s/%d' % (ROMS_DIR, firm['id'])
        if not os.path.exists(firmname):
            return
        with open(firmname, 'rb') as fin:
            return fin.read()

    def get_firmware(self, firm):
        rsp = self.local_firm_exist(firm)
        if rsp:
            return rsp
        self.consolelog(u'本地不存在，网上下载')
        rsp = self.down_firmfile(firm)
        if not rsp:
            self.consolelog(u'下载失败')
            return
        return rsp

    def get_embed_firms(self, fileName):
        # ":/resources/blank.bin"
        baseName = ':/resources/'
        bFile = QFile(baseName + fileName)
        if not bFile.open(QIODevice.ReadOnly):
            self.consolelog(u'读取内嵌资源 %s 出错！' % fileName)
            return
        content = bFile.readAll()
        return bytearray(content)

    def get_all_embed_firms(self):
        blank = self.get_embed_firms('blank.bin')
        if not blank:
            return
        boot = self.get_embed_firms('boot_v1.7.bin')
        if not boot:
            return
        esp_init_data = self.get_embed_firms('esp_init_data_default.bin')
        if not esp_init_data:
            return
        return {
            'blank': blank,
            'boot': boot,
            'init': esp_init_data,
        }

    def go(self):
        if self.action_state() == 'FLASH':
            self.change_btn_to_abort()
            self.go_flash()
        elif self.action_state() == 'ABORT':
            self.change_btn_to_flash()
            self.go_abort()

    def go_abort(self):
        self.consolelog(u'准备终止烧录过程')
        self.flasher.abort_flash_sig.emit()

    def go_flash(self):            
        firmwares = self.get_all_embed_firms()
        device = self.ui.com_box.currentText()
        firmware = self.ui.firm_box.itemData(self.ui.firm_box.currentIndex())
        firmobj = firmware.toPyObject()[0]
        firmfile = self.get_firmware(firmobj)
        firmwares['app1'] = firmfile
        self.consolelog(u'固件准备完毕，准备烧录到 %s' % device)
        self.flasher.begin_flash_sig.emit(device, firmwares)

    def show_ctx_menu(self):
        self.pop_menu.exec_(QCursor.pos())

    def clear_console(self):
        self.console_clear()


def main():
    app = QApplication(sys.argv)
    widget = FlashDlg()
    widget.show()
    app.exec_()


if __name__ == '__main__':
    main()


'''
venv\Lib\site-packages\PyQt4\pyuic4.bat main.ui >dlg.py
venv\Lib\site-packages\PyQt4\pyrcc4.exe resources.qrc >resources_rc.py
pyinstaller -F --noupx -w --win-no-prefer-redirects --clean --icon espush.ico flasher.py
'''
