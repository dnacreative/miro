from copy import copy
from datetime import datetime, timedelta
from gettext import gettext as _
from math import ceil
from xhtmltools import unescape,xhtmlify
from xml.sax.saxutils import unescape
import locale
import os
import shutil
import traceback

from feedparser import FeedParserDict

from database import DDBObject, defaultDatabase
from iconcache import IconCache
from templatehelper import escape
import downloader
import config
import dialogs
import eventloop
import filters
import prefs
import resource
import views

def updateUandA (feed):
    # Not toplevel to avoid a dependency loop at load time.
    import feed as feed_mod
    feed_mod.updateUandA (feed)

_charset = locale.getpreferredencoding()

class Item(DDBObject):
    """An item corresponds to a single entry in a feed. It has a single url
    associated with it.
    """

    def __init__(self, feed_id, entry, linkNumber = 0):
        self.feed_id = feed_id
        self.seen = False
        self.downloader = None
        self.autoDownloaded = False
        self.pendingManualDL = False
        self.downloadedTime = None
        self.watchedTime = None
        self.pendingReason = ""
        self.entry = entry
        self.expired = False
        self.keep = False

        self.iconCache = IconCache(self)
        
        # linkNumber is a hack to make sure that scraped items at the
        # top of a page show up before scraped items at the bottom of
        # a page. 0 is the topmost, 1 is the next, and so on
        self.linkNumber = linkNumber
        self.creationTime = datetime.now()
        DDBObject.__init__(self)
        updateUandA(self.getFeed())

    # Unfortunately, our database does not scale well with many views,
    # so we have this hack to make sure that unwatched and available
    # get updated when an item changes
    def signalChange(self, needsSave=True, needsUpdateUandA=True):
        DDBObject.signalChange(self, needsSave=needsSave)
        if needsUpdateUandA:
            try:
                # If the feed has been deleted, getFeed will throw an exception
                updateUandA(self.getFeed())
            except:
                pass

    #
    # Returns True iff this item has never been viewed in the interface
    # Note the difference between "viewed" and seen
    def getViewed(self):
        return self.creationTime <= self.getFeed().lastViewed 

    ##
    # Returns the first video enclosure in the item
    def getFirstVideoEnclosure(self):
        self.confirmDBThread()
        try:
            enclosures = self.entry.enclosures
        except (KeyError, AttributeError):
            return None
        for enclosure in enclosures:
            if isVideoEnclosure(enclosure):
                return enclosure
        return None

    ##
    # Returns the URL associated with the first enclosure in the item
    def getURL(self):
        self.confirmDBThread()
        videoEnclosure = self.getFirstVideoEnclosure()
        if videoEnclosure is not None and 'url' in videoEnclosure:
            return videoEnclosure['url']
        else:
            return ''

    def hasSharableURL(self):
        """Does this item have a URL that the user can share with others?

        This returns True when the item has a non-file URL.
        """
        url = self.getURL()
        return url != '' and not url.startswith("file:")

    ##
    # Returns the feed this item came from
    def getFeed(self):
        return self.dd.getObjectByID(self.feed_id)

    def feedExists(self):
        return self.dd.idExists(self.feed_id)

    ##
    # Moves this item to another feed.
    def setFeed(self, feed_id):
        self.feed_id = feed_id
        self.signalChange()


    ##
    # Marks this item as expired
    def expire(self):
        UandA = self.getUandA()
        self.confirmDBThread()
        self.stopDownload()
        self.expired = True
        self.seen = self.keep = self.pendingManualDL = False
        self.signalChange(needsUpdateUandA = (UandA != self.getUandA()))

    def getExpirationString(self):
        """Get the expiration time a string to display to the user."""
        expireTime = self.getExpirationTime()
        if expireTime is None:
            return ""
        else:
            exp = expireTime - datetime.now()
            if exp.days > 0:
                time = _("%d days") % exp.days
            elif exp.seconds > 3600:
                time = _("%d hrs") % (ceil(exp.seconds/3600.0))
            else:
                time = _("%d min") % (ceil(exp.seconds/60.0))
        return _('Expires: %s') % time

    def _getStateCSSClassAndState(self):
        """Does the work for both getStateCSSClass() and getStateString().
        It's in one function to make sure that they stay in sync
        """

        if self.isPendingAutoDownload():
            return 'pending-autdownload', _('Pending Auto Download')
        elif self.isFailedDownload():
            return 'failed-download', self.getFailureReason()
        elif self.isDownloaded():
            if self.getState() == 'newly-downloaded':
                return 'newly-downloaded', _('UNWATCHED')
            elif self.getState() == 'expiring':
                return 'expiring', self.getExpirationString()
            else:
                return '', ''
        elif not self.getViewed():
            return 'new', _('NEW')
        else:
            return '', ''

    def getStateCSSClass(self):
        """Get the CSS class to display our state string."""
        return self._getStateCSSClassAndState()[0]

    def getStateString(self):
        """Get a human-readable string to display to the user."""
        return self._getStateCSSClassAndState()[1]

    def getUandA(self):
        """Get whether this item is new, or newly-downloaded, or neither."""
        state = self.getStateCSSClass()
        if state == 'new':
            return (0, 1)
        elif state == 'newly-downloaded':
            return (1, 0)
        else:
            return (0, 0)

    def getExpirationTime(self):
        """Get the time when this item will expire. 
        Returns a datetime object,  or None if it doesn't expire.
        """

        self.confirmDBThread()
        if self.watchedTime is None or not self.isDownloaded():
            return None
        ufeed = self.getFeed()
        if ufeed.expire == 'never' or (ufeed.expire == 'system'
                and config.get(prefs.EXPIRE_AFTER_X_DAYS) <= 0):
            return None
        else:
            if ufeed.expire == "feed":
                expireTime = ufeed.expireTime
            elif ufeed.expire == "system":
                expireTime = timedelta(days=config.get(prefs.EXPIRE_AFTER_X_DAYS))
            return self.watchedTime + expireTime

    ##
    # returns true iff video has been seen
    # Note the difference between "viewed" and "seen"
    def getSeen(self):
        self.confirmDBThread()
        return self.seen

    ##
    # Marks the item as seen
    def markItemSeen(self):
        self.confirmDBThread()
        self.seen = True
        if self.watchedTime is None:
            self.watchedTime = datetime.now()
        self.signalChange()

    def getRSSID(self):
        self.confirmDBThread()
        return self.entry["id"]

    def setAutoDownloaded(self,autodl = True):
        self.confirmDBThread()
        self.autoDownloaded = autodl
        self.signalChange()

    def getPendingReason(self):
        self.confirmDBThread()
        return self.pendingReason

    ##
    # Returns true iff item was auto downloaded
    def getAutoDownloaded(self):
        self.confirmDBThread()
        return self.autoDownloaded

    ##
    # Returns the linkNumber
    def getLinkNumber(self):
        self.confirmDBThread()
        return self.linkNumber

    def download(self,autodl=False):
        eventloop.addIdle(lambda : self.actualDownload(autodl), "Spawning Download %s" % self.getURL())

    ##
    # Starts downloading the item
    def actualDownload(self,autodl=False):
        self.confirmDBThread()
        manualDownloadCount = views.manualDownloads.len()
        self.expired = self.keep = self.seen = False

        if ((not autodl) and 
                manualDownloadCount >= config.get(prefs.MAX_MANUAL_DOWNLOADS)):
            self.pendingManualDL = True
            self.pendingReason = "queued for download"
            self.signalChange()
            return
        else:
            self.setAutoDownloaded(autodl)
            self.pendingManualDL = False

        if self.downloader is None:
            self.downloader = downloader.getDownloader(self)
        self.downloader.start()
        self.signalChange()

    def isPendingManualDownload(self):
        self.confirmDBThread()
        return self.pendingManualDL

    def isEligibleForAutoDownload(self):
        self.confirmDBThread()
        if self.getState() not in ('new', 'not-downloaded'):
            return False
        if self.downloader and self.downloader.getState() in ('failed',
                'stopped', 'paused'):
            return False
        ufeed = self.getFeed()
        if ufeed.getEverything:
            return True
        pubDate = self.getPubDateParsed()
        return pubDate >= ufeed.startfrom and pubDate != datetime.max

    def isPendingAutoDownload(self):
        return (self.getFeed().isAutoDownloadable() and
                self.isEligibleForAutoDownload())

    def isFailedDownload(self):
        return self.downloader and self.downloader.getState() == 'failed'

    ##
    # Returns a link to the thumbnail of the video
    def getThumbnailURL(self):
        self.confirmDBThread()
        # Try to get the thumbnail specific to the video enclosure
        videoEnclosure = self.getFirstVideoEnclosure()
        if videoEnclosure is not None:
            try:
                return videoEnclosure["thumbnail"]["url"]
            except:
                pass 
        # Try to get any enclosure thumbnail
        for enclosure in self.entry.enclosures:
            try:
                return enclosure["thumbnail"]["url"]
            except KeyError:
                pass
        # Try to get the thumbnail for our entry
        try:
            return self.entry["thumbnail"]["url"]
        except:
            return None

    def getThumbnail (self):
        self.confirmDBThread()
        if self.iconCache.isValid():
            basename = os.path.basename(self.iconCache.getFilename())
            return resource.iconCacheUrl(basename)
        else:
            return resource.url("images/thumb.png")
    ##
    # returns the title of the item
    def getTitle(self):
        try:
            return self.entry.title
        except:
            try:
                enclosure = self.getFirstVideoEnclosure()
                return enclosure["url"]
            except:
                return ""

    ##
    # Returns valid XHTML containing a description of the video
    def getDescription(self):
        self.confirmDBThread()
        try:
            enclosure = self.getFirstVideoEnclosure()
            return xhtmlify('<span>'+unescape(enclosure["text"])+'</span>')
        except:
            try:
                return xhtmlify('<span>'+unescape(self.entry.description)+'</span>')
            except:
                return '<span />'

    def looksLikeTorrent(self):
        """Returns true if we think this item is a torrent.  (For items that
        haven't been downloaded this uses the file extension which isn't
        totally reliable).
        """

        if self.downloader is not None:
            return self.downloader.getType() == 'bittorrent'
        else:
            return self.getURL().endswith('.torrent')

    ##
    # Returns formatted XHTML with release date, duration, format, and size
    def getDetails(self):
        details = []
        reldate = self.getReleaseDate()
        duration = self.getDuration()
        format = self.getFormat()
        size = self.getSizeForDisplay()
        if len(reldate) > 0:
            details.append('<span class="details-date">%s</span>' % escape(reldate))
        if len(duration) > 0:
            details.append('<span class="details-duration">%s</span>' % escape(duration))
        if len(format) > 0:
            details.append('<span class="details-format">%s</span>' % escape(format))
        if len(size) > 0:
            details.append('<span class="details-size">%s</span>' % escape(size))
        if self.looksLikeTorrent():
            details.append('<span class="details-torrent" il8n:translate="">TORRENT</span>')
        out = ' - '.join(details)
        return out

    ##
    # Stops downloading the item
    def stopDownload(self):
        self.confirmDBThread()
        if self.downloader is not None:
            self.downloader.removeItem(self)
            self.downloader = None
            self.signalChange()

    def getState(self):
        """Get the state of this item.  The state will be on of the following:

        * new -- User has never seen this item
        * not-downloaded -- User has seen the item, but not downloaded it
        * downloading -- Item is currently downloading
        * newly-downloaded -- Item has been downoladed, but not played
        * expiring -- Item has been played and is set to expire
        * saved -- Item has been played and has been saved
        * expired -- Item has expired.
        """
        
        self.confirmDBThread()
        # FIXME, 'failed', and 'paused' should get download icons.  The user
        # should be able to restart or cancel them (put them into the stopped
        # state).
        if (self.downloader is None  or 
                self.downloader.getState() in ('failed', 'stopped', 'paused')):
            if self.pendingManualDL:
                return 'downloading'
            elif self.expired:
                return 'expired'
            elif not self.getViewed():
                return 'new'
            else:
                return 'not-downloaded'
        elif not self.downloader.isFinished():
            return 'downloading'
        elif not self.seen:
            return 'newly-downloaded'
        elif not self.keep:
            return 'expiring'
        else:
            return 'saved'

    def getChannelCategory(self):
        """Get the category to use for the channel template.  
        
        This method is similar to getState(), but has some subtle differences.
        getState() is used by the download-item template and is usually more
        useful to determine what's actually happening with an item.
        getChannelCategory() is used by by the channel template to figure out
        which heading to put an item under.

        * downloading and not-downloaded are grouped together as
          not-downloaded
        * Items are always new if their feed hasn't been marked as viewed
          after the item's pub date.  This is so that when a user gets a list
          of items and starts downloading them, the list doesn't reorder
          itself.
        """

        self.confirmDBThread()
        if not self.getViewed():
            return 'new'
        elif self.downloader is None or not self.downloader.isFinished():
            if self.expired:
                return 'expired'
            else:
                return 'not-downloaded'
        elif not self.seen:
            return 'newly-downloaded'
        elif not self.keep:
            return 'expiring'
        else:
            return 'saved'

    def isDownloaded(self):
        return self.getState() in ("newly-downloaded", "expiring", "saved")

    def showSaveButton(self):
        return (self.getState() in ('newly-downloaded', 'expiring') and
                self.getExpirationTime() is not None)

    def getFailureReason(self):
        self.confirmDBThread()
        if self.downloader is not None:
            return self.downloader.getReasonFailed()
        else:
            return ""
    
    ##
    # Returns the size of the item to be displayed. If the item has a
    # corresponding downloaded enclosure we use the pysical size of the file,
    # otherwise we use the RSS enclosure tag values.
    def getSizeForDisplay(self):
        fname = self.getFilename()
        try:
            size = os.stat(fname)[6]
            return self.sizeFormattedForDisplay(size)
        except:
            return self.getEnclosuresSize()
    
    ##
    # Returns the total size of all enclosures in bytes
    def getEnclosuresSize(self):
        size = 0
        try:
            size = int(self.getFirstVideoEnclosure()['length'])
        except:
            pass
        return self.sizeFormattedForDisplay(size)

    ##
    # returns status of the download in plain text
    def getCurrentSize(self):
        if self.downloader is not None:
            size = self.downloader.getCurrentSize()
        else:
            size = 0
        if size == 0:
            return ""
        return self.sizeFormattedForDisplay(size)

    ##
    # Returns a byte size formatted for display
    def sizeFormattedForDisplay(self, bytes, emptyForZero=True):
        if bytes > (1 << 30):
            return "%1.1fGB" % (bytes / (1024.0 * 1024.0 * 1024.0))
        elif bytes > (1 << 20):
            return "%1.1fMB" % (bytes / (1024.0 * 1024.0))
        elif bytes > (1 << 10):
            return "%1.1fKB" % (bytes / 1024.0)
        elif bytes > 1:
            return "%0.0fB" % bytes
        else:
            if emptyForZero:
                return ""
            else:
                return "n/a"

    ##
    # Returns the download progress in absolute percentage [0.0 - 100.0].
    def downloadProgress(self):
        progress = 0
        self.confirmDBThread()
        if self.downloader is None:
            return 0
        else:
            size = self.downloader.getTotalSize()
            dled = self.downloader.getCurrentSize()
            if size == 0:
                return 0
            else:
                return (100.0*dled) / size

    ##
    # Returns the width of the progress bar corresponding to the current
    # download progress. This doesn't really belong here and even forces
    # to use a hardcoded constant, but the templating system doesn't 
    # really leave any other choice.
    def downloadProgressWidth(self):
        fullWidth = 92  # width of resource:channelview-progressbar-bg.png - 2
        progress = self.downloadProgress() / 100.0
        if progress == 0:
            return 0
        return int(progress * fullWidth)

    ##
    # Returns string containing three digit percent finished
    # "000" through "100".
    def threeDigitPercentDone(self):
        return '%03d' % int(self.downloadProgress())

    ##
    # Returns string with estimate time until download completes
    def downloadETA(self):
        if self.downloader is not None:
            secs = self.downloader.getETA()
        elif self.pendingManualDL:
            return self.pendingReason
        else:
            secs = 0
        if secs == 0:
            return 'starting up...'
        elif (secs < 120):
            return '%1.0f secs left - ' % secs
        elif (secs < 6000):
            return '%1.0f mins left - ' % ceil(secs/60.0)
        else:
            return '%1.1f hours left - ' % ceil(secs/3600.0)

    ##
    # Returns the download rate
    def downloadRate(self):
        rate = 0
        unit = "k/s"
        if self.downloader is not None:
            rate = self.downloader.getRate()
        else:
            rate = 0
        rate /= 1024
        if rate > 1024:
            rate /= 1024
            unit = "m/s"
        if rate > 1024:
            rate /= 1024
            unit = "g/s"
            
        return "%d%s" % (rate, unit)

    ##
    # Returns the published date of the item
    def getPubDate(self):
        self.confirmDBThread()
        try:
            return datetime(*self.entry.modified_parsed[0:7]).strftime("%b %d %Y").decode(_charset)
        except:
            return ""
    
    ##
    # Returns the published date of the item as a datetime object
    def getPubDateParsed(self):
        self.confirmDBThread()
        try:
            return datetime(*self.entry.modified_parsed[0:7])
        except:
            return datetime.max # Is this reasonable? It should
                                # avoid type issues for now, if
                                # nothing else

    ##
    # returns the date this video was released or when it was published
    def getReleaseDate(self):
        try:
            return self.releaseDate
        except:
            try:
                self.releaseDate = datetime(*self.getFirstVideoEnclosure().modified_parsed[0:7]).strftime("%b %d %Y").decode(_charset)
                return self.releaseDate
            except:
                try:
                    self.releaseDate = datetime(*self.entry.modified_parsed[0:7]).strftime("%b %d %Y").decode(_charset)
                    return self.releaseDate
                except:
                    self.releaseDate = ""
                    return self.releaseDate
            

    ##
    # returns the date this video was released or when it was published
    def getReleaseDateObj(self):
        if hasattr(self,'releaseDateObj'):
            return self.releaseDateObj
        self.confirmDBThread()
        try:
            self.releaseDateObj = datetime(*self.getFirstVideoEnclosure().modified_parsed[0:7])
        except:
            try:
                self.releaseDateObj = datetime(*self.entry.modified_parsed[0:7])
            except:
                self.releaseDateObj = datetime.min
        return self.releaseDateObj

    ##
    # returns string with the play length of the video
    def getDuration(self, emptyIfZero=True):
        secs = 0
        #FIXME get this from VideoInfo
        if secs == 0:
            if emptyIfZero:
                return ""
            else:
                return "n/a"
        if (secs < 120):
            return '%1.0f secs' % secs
        elif (secs < 6000):
            return '%1.0f mins' % ceil(secs/60.0)
        else:
            return '%1.1f hours' % ceil(secs/3600.0)

    ##
    # returns string with the format of the video
    KNOWN_MIME_TYPES = ('audio', 'video')
    KNOWN_MIME_SUBTYPES = ('mov', 'wmv', 'mp4', 'mp3', 'mpg', 'mpeg', 'avi')
    def getFormat(self, emptyForUnknown=True):
        try:
            enclosure = self.entry['enclosures'][0]
            if enclosure.has_key('type') and len(enclosure['type']) > 0:
                type, subtype = enclosure['type'].split('/')
                if type.lower() in self.KNOWN_MIME_TYPES:
                    return subtype.split(';')[0].upper()
            else:
                extension = enclosure['url'].split('.')[-1].lower()
                if extension in self.KNOWN_MIME_SUBTYPES:
                    return extension.upper()
        except:
            pass
        if emptyForUnknown:
            return ""
        else:
            return "n/a"

    ##
    # return keyword tags associated with the video separated by commas
    def getTags(self):
        self.confirmDBThread()
        try:
            return self.entry.categories.join(", ")
        except:
            return ""

    ##
    # return the license associated with the video
    def getLicence(self):
        self.confirmDBThread()
        try:
            return self.entry.license
        except:
            try:
                return self.getFeed().getLicense()
            except:
                return ""

    ##
    # return the people associated with the video, separated by commas
    def getPeople(self):
        ret = []
        self.confirmDBThread()
        try:
            for role in self.getFirstVideoEnclosure().roles:
                for person in self.getFirstVideoEnclosure().roles[role]:
                    ret.append(person)
            for role in self.entry.roles:
                for person in self.entry.roles[role]:
                    ret.append(person)
        except:
            pass
        return ', '.join(ret)

    ##
    # returns the URL of the webpage associated with the item
    def getLink(self):
        self.confirmDBThread()
        try:
            return self.entry.link
        except:
            return ""

    ##
    # returns the URL of the payment page associated with the item
    def getPaymentLink(self):
        self.confirmDBThread()
        try:
            return self.getFirstVideoEnclosure().payment_url
        except:
            try:
                return self.entry.payment_url
            except:
                return ""

    ##
    # returns a snippet of HTML containing a link to the payment page
    # HTML has already been sanitized by feedparser
    def getPaymentHTML(self):
        self.confirmDBThread()
        try:
            ret = self.getFirstVideoEnclosure().payment_html
        except:
            try:
                ret = self.entry.payment_html
            except:
                ret = ""
        # feedparser returns escaped CDATA so we either have to change its
        # behavior when it parses dtv:paymentlink elements, or simply unescape
        # here...
        return '<span>' + unescape(ret) + '</span>'

    ##
    # Updates an item with new data
    #
    # @param entry a dict object containing the new data
    def update(self, entry):
        UandA = self.getUandA()
        self.confirmDBThread()
        try:
            self.entry = entry
            self.iconCache.requestUpdate()
        finally:
            self.signalChange(needsUpdateUandA = (UandA != self.getUandA()))

    def onDownloadFinished(self):
        """Called when the download for this item finishes."""

        self.confirmDBThread()
        self.downloadedTime = datetime.now()
        self.keep = (self.getFeed().expire == "never")
        self.signalChange()

    def save(self):
        self.confirmDBThread()
        self.keep = True
        self.signalChange()

    ##
    # gets the time the video was downloaded
    # Only valid if the state of this item is "finished"
    def getDownloadedTime(self):
        if self.downloadedTime is None:
            return datetime.min
        else:
            return self.downloadedTime

    ##
    # Returns the filename of the first downloaded video or the empty string
    # NOTE: this will always return the absolute path to the file.
    def getFilename(self):
        self.confirmDBThread()
        try:
            return self.downloader.getFilename()
        except:
            return ""

    def getRSSEntry(self):
        self.confirmDBThread()
        return self.entry

    def remove(self):
        if self.downloader is not None:
            self.downloader.remove()
            self.downloader = None
        if self.iconCache is not None:
            self.iconCache.remove()
            self.iconCache = None
        DDBObject.remove(self)

    def reconnectDownloader(self):
        """This is called after we restore the database.  Since we don't store
        references between objects, we need a way to reconnect downloaders to
        the items after the restore.
        """

        if self.downloader is None:
            self.downloader = downloader.getDownloader(self, create=False)
            if self.downloader is not None:
                self.signalChange(needsSave=False)
        else:
            # Do this here instead of onRestore in case the feed
            # hasn't been loaded yet.  signalChange calls this
            # function, so we only need to do it if we don't call
            # signalChange.
            updateUandA(self.getFeed())

    ##
    # Called by pickle during serialization
    def onRestore(self):
        if (self.iconCache == None):
            self.iconCache = IconCache (self)
        else:
            self.iconCache.dbItem = self
            self.iconCache.requestUpdate()
        self.downloader = None

    def __str__(self):
        return "Item - %s" % self.getTitle()

def reconnectDownloaders():
    for item in views.items:
        item.reconnectDownloader()

def getEntryForFile(filename):
    return FeedParserDict({'title':os.path.basename(filename),
            'enclosures':[{'url': 'file://%s' % filename}]})

##
# An Item that exists as a local file
class FileItem(Item):

    def __init__(self,feed_id,filename):
        filename = os.path.abspath(filename)
        self.filename = filename
        self.deleted = False
        Item.__init__(self, feed_id, getEntryForFile(filename))

    def getState(self):
        if self.deleted:
            return "expired"
        elif self.getSeen():
            return "saved"
        else:
            return "newly-downloaded"

    def showSaveButton(self):
        return False

    def getViewed(self):
        return True

    def expire(self):
        title = _("Removing %s") % (os.path.basename(self.filename))
        description = _("Would you like to delete this file or just remove "
                "its entry from My Collection?")
        d = dialogs.ThreeChoiceDialog(title, description,
                dialogs.BUTTON_REMOVE_ENTRY, dialogs.BUTTON_DELETE_FILE,
                dialogs.BUTTON_CANCEL)
        def callback(dialog):
            if dialog.choice == dialogs.BUTTON_DELETE_FILE:
                try:
                    os.remove(self.filename)
                except:
                    print "WARNING: Error deleting %s" % self.filename
                    traceback.print_exc()
                self.remove()
            elif dialog.choice == dialogs.BUTTON_REMOVE_ENTRY:
                self.confirmDBThread()
                self.deleted = True
                self.signalChange()

        d.run(callback)

    def getDownloadedTime(self):
        self.confirmDBThread()
        try:
            return datetime.fromtimestamp(os.getctime(self.filename))
        except:
            return datetime.min

    def getFilename(self):
        try:
            return self.filename
        except:
            return ""

    def migrate(self, newDir):
        self.confirmDBThread()
        try:
            if os.path.exists(self.filename):
                newFilename = os.path.join(newDir, os.path.basename(self.filename))
                try:
                    shutil.move(self.filename, newFilename)
                except IOError, e:
                    print "WARNING: Error moving %s to %s (%s)" % (self.filename,
                            newFilename, e)
                else:
                    self.filename = newFilename
        finally:
             self.signalChange()

def isVideoEnclosure(enclosure):
    """
    Pass an enclosure dictionary to this method and it will return a boolean
    saying if the enclosure is a video or not.
    """
    hasVideoType = (enclosure.has_key('type') and
        (enclosure['type'].startswith('video/') or
         enclosure['type'].startswith('audio/') or
         enclosure['type'] == "application/ogg" or
         enclosure['type'] == "application/x-annodex" or
         enclosure['type'] == "application/x-bittorrent"))
    hasVideoExtension = (enclosure.has_key('url') and
        ((len(enclosure['url']) > 4 and
          enclosure['url'][-4:].lower() in ['.mov','.wmv','.mp4', '.m4v',
                                      '.mp3','.ogg','.anx','.mpg','.avi']) or
         (len(enclosure['url']) > 8 and
          enclosure['url'][-8:].lower() == '.torrent') or
         (len(enclosure['url']) > 5 and
          enclosure['url'][-5:].lower() == '.mpeg')))
    return hasVideoType or hasVideoExtension
